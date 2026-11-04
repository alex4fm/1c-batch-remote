"""Coordinate UUID registration, process ownership, snapshots and persistent results."""

import hashlib
import os
import threading
import time
from uuid import uuid4

from executor_config import ApiError, checked_path
from operation_store import OperationStore, now, request_uuid
from process_executor import ProcessExecutor, ProcessRun


class Operation:
    def __init__(self, service, identifier, run=None):
        self.service = service
        self.identifier = identifier
        self.run = run
        self.log_path = service.store.path(identifier, "output.log")

    def snapshot(self):
        return self.service.store.read(self.identifier)

    def wait(self, timeout=None):
        if self.run is not None:
            self.run.wait(timeout)
        record = self.snapshot()
        if not record["terminal"]:
            raise TimeoutError("The operation is still active.")
        return record

    def stop(self):
        if self.run is not None:
            self.run.stop()

    def await_started(self, timeout=25):
        if self.run is not None and not self.run._started.wait(timeout):
            raise ApiError(503, "StartupPending", "Operation is registered; launch is not confirmed yet.")
        record = self.snapshot()
        if record["state"] == "starting":
            raise ApiError(503, "StartupPending", "Operation is registered; launch is not confirmed yet.")
        return record


class OperationService:
    def __init__(self, settings, busy=None):
        self.settings = settings
        self.store = OperationStore(settings)
        self.busy = busy if busy is not None else threading.Lock()
        try:
            self.runner = ProcessExecutor(settings, busy=self.busy)
        except BaseException:
            self.store.close()
            raise
        self._registration = threading.RLock()
        self._handles = {}
        self._closed = False
        self._close_done = threading.Event()

    def submit(self, project_id, command, request_id):
        """Return (operation, created) atomically for the HTTP response semantics."""
        identifier = request_uuid(request_id)
        with self._registration:
            existing = self.store.find(identifier)
            return self.start(project_id, command, identifier), existing is None

    def start(self, project_id, command, request_id=None):
        identifier = request_uuid(request_id if request_id is not None else str(uuid4()))
        with self._registration:
            if self._closed:
                raise ApiError(503, "ExecutorStopped", "Operation executor has stopped.")
            self._handles = {key: handle for key, handle in self._handles.items() if not handle.run._done.is_set()}
            self.store.check_modifiable()
            existing = self.store.find(identifier)
            if existing is not None:
                if existing["projectId"] != project_id or existing["command"] != command:
                    raise ApiError(409, "RequestIdConflict", "requestId was registered with different parameters.")
                # Known UUIDs are handled before busy and before current project settings.
                return self._handles.get(identifier) or Operation(self, identifier)
            prepared = []

            def prepare():
                project = self.settings.project
                record = self.store.register(identifier, project_id, command, project.output_files)

                def persist(values):
                    values = {**values, "logAvailable": self.store.path(identifier, "output.log").is_file()}
                    self.store.update(identifier, **values)

                run = ProcessRun(project_id, command, self.store.path(identifier, "output.log"),
                    persist=persist, finalize=lambda state: self._collect(identifier, project, state, run._finalization_deadline),
                    job_name=record["jobName"], fault=lambda exc: self.store.unavailable(str(exc)))
                prepared.append(run)
                return run

            self.runner.settings = self.settings
            try:
                run = self.runner.start(project_id, command, prepare=prepare)
            except Exception:
                if prepared and self.store.problem is None:
                    self.store.update(identifier, state="failed", terminal=True, finishedAt=now(),
                        error=dict(code="ExecutionFailed", message="Could not start background worker."))
                raise
            operation = Operation(self, identifier, run)
            self._handles[identifier] = operation
            return operation

    def _collect(self, identifier, project, state, deadline):
        if state != "succeeded":
            return {"outputs": []}
        records = []
        for relative in project.output_files:
            source = checked_path(self.settings.workspace, project.root.joinpath(*relative.split("/")))
            if not source.is_file():
                raise OSError(f"Expected output file is missing: {relative}")
            target = self.store.path(identifier, "outputs", *relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            count, digest = 0, hashlib.sha256()
            # The snapshot is invisible in status until all copies and syncs finish.
            with source.open("rb") as input_stream, target.open("xb") as output:
                while block := input_stream.read(64 * 1024):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Result collection time limit exceeded.")
                    count += len(block)
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if time.monotonic() >= deadline:
                raise TimeoutError("Result collection time limit exceeded.")
            records.append(dict(path=relative, size=count, sha256=digest.hexdigest(), available=True))
        return {"outputs": records}

    def close(self):
        with self._registration:
            already_closed = self._closed
            self._closed = True
        if already_closed:
            self._close_done.wait()
            return
        self.store.stop_changes()
        try:
            self.runner.close()
        finally:
            try:
                # Uploads use the same gate. Keep runtime ownership until any
                # upload that already passed its guard has finished too.
                with self.busy:
                    self.store.close()
            finally:
                self._close_done.set()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
