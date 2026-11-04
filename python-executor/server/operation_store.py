"""Versioned, fail-closed operation records and OS ownership of a local runtime."""

from contextlib import contextmanager
import codecs
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from uuid import UUID, uuid4

from executor_config import ApiError, checked_path


ACTIVE = {"starting", "running", "finalizing"}
TERMINAL = {"succeeded", "failed", "timed_out", "interrupted"}


def now():
    return datetime.now(timezone.utc).isoformat()


def request_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value.lower():
            raise ValueError
        return value.lower()
    except ValueError:
        raise ApiError(400, "InvalidRequestId", "requestId must be a canonical UUID.") from None


def fingerprint(project_id, command):
    data = json.dumps([project_id, command], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Duplicate JSON key")
        data[key] = value
    return data


def _date(value):
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp")
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError("Timestamp needs timezone")
    return date


class RuntimeLock:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            raise ApiError(503, "RuntimeInUse", "Another executor owns this runtime.") from exc

    def close(self):
        if self.fd is not None:
            # Closing the descriptor releases the OS lock, including on owner death.
            os.close(self.fd)
            self.fd = None


class OperationStore:
    def __init__(self, settings):
        self.settings = settings
        self.workspace = settings.workspace
        self.root = checked_path(self.workspace, settings.runtime or settings.staging.parent / "runtime")
        self.operations = self.root / "operations"
        self.mutex = threading.RLock()
        self.problem = None
        self.closed = False
        self.accepting_changes = True
        self._known = set()
        self._readers = set()
        self.owner = None
        self.store_id = None
        self.root.mkdir(parents=True, exist_ok=True)
        self.owner = RuntimeLock(checked_path(self.workspace, self.root / "executor.lock"))
        try:
            with self._io():
                marker = self.root / "store.json"
                if marker.exists():
                    data = self._json(marker)
                    if type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
                        raise ValueError("Unknown store format")
                    self.store_id = request_uuid(data["storeId"])
                    if not checked_path(self.workspace, self.operations).is_dir():
                        raise ValueError("Operations directory is missing")
                else:
                    if self.operations.exists() and any(self.operations.iterdir()):
                        raise ValueError("Missing store marker in a nonempty runtime")
                    self.operations.mkdir(exist_ok=True)
                    self.store_id = str(uuid4())
                    self._atomic(marker, dict(schemaVersion=1, storeId=self.store_id))
                self._purge_pending()
                records = self._scan()
                # Validate all records before changing any of them.
                from windows_job import stop_previous_job
                for record in records:
                    if record["state"] in ACTIVE:
                        stop_previous_job(record["jobName"], settings.limits.result_timeout_seconds)
                        self._update(record["requestId"], dict(state="interrupted", terminal=True,
                            finishedAt=now(), recoveredAt=now(), exitCode=None,
                            error=dict(code="ExecutionInterrupted", message="Executor restarted without a saved result."),
                            logAvailable=self.path(record["requestId"], "output.log").is_file()))
                self._remove_expired_on_start()
        except ApiError:
            # Keep ownership and expose 503 until an administrator repairs and restarts.
            pass

    def unavailable(self, message):
        self.problem = message
        return ApiError(503, "StorageUnavailable", "Operation storage is unavailable.", {"reason": message})

    @contextmanager
    def _io(self):
        with self.mutex:
            try:
                yield
            except Exception as exc:
                if isinstance(exc, ApiError) and exc.code == "StorageUnavailable" and (self.problem or self.closed):
                    raise
                if isinstance(exc, ApiError) and exc.code in {"UnknownOperation", "ArtifactsExpired", "UnknownOutput", "RequestIdConflict", "ExecutorStopped"}:
                    raise
                raise self.unavailable(str(exc)) from exc

    def _ready(self):
        if self.closed or self.problem:
            raise ApiError(503, "StorageUnavailable", "Operation storage is unavailable.", {"reason": self.problem or "Store is closed."})
        marker = self._json(self.root / "store.json")
        if type(marker.get("schemaVersion")) is not int or marker != dict(schemaVersion=1, storeId=self.store_id):
            raise ValueError("Store identity changed")
        if not checked_path(self.workspace, self.operations).is_dir():
            raise ValueError("Operations directory is missing")

    def _json(self, path):
        path = checked_path(self.workspace, path)
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Status JSON is too large")
        result = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if not isinstance(result, dict):
            raise ValueError("Status must be an object")
        return result

    def _atomic(self, destination, data):
        destination = checked_path(self.workspace, destination)
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
        if len(payload) > 1024 * 1024:
            raise ValueError("Status JSON is too large")
        fd, temporary = tempfile.mkstemp(prefix=".status-", suffix=".tmp", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            checked_path(self.workspace, destination)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def job_name(self, identifier):
        return "Global\\1c-batch-" + self.store_id + "-" + identifier

    def path(self, identifier, *parts):
        identifier = request_uuid(identifier)
        target = self.operations / identifier
        for part in parts:
            target /= part
        target = Path(os.path.abspath(target))
        try:
            target.relative_to(self.operations / identifier)
        except ValueError:
            raise ApiError(403, "ForbiddenPath", "Path is outside the operation directory.") from None
        return checked_path(self.workspace, target)

    def _validate(self, identifier, data):
        if type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
            raise ValueError("Unknown status format")
        if data["requestId"] != identifier or request_uuid(identifier) != identifier:
            raise ValueError("Status UUID does not match its directory")
        if not isinstance(data["projectId"], str) or not data["projectId"]:
            raise ValueError("Invalid saved projectId")
        if not isinstance(data["command"], str) or not data["command"].strip() or "\0" in data["command"]:
            raise ValueError("Invalid saved command")
        if data["fingerprint"] != fingerprint(data["projectId"], data["command"]):
            raise ValueError("Saved parameters fingerprint mismatch")
        if data["jobName"] != self.job_name(identifier):
            raise ValueError("Invalid saved job name")
        state = data["state"]
        if state not in ACTIVE | TERMINAL or type(data["terminal"]) is not bool or data["terminal"] != (state in TERMINAL):
            raise ValueError("Invalid state or terminal flag")
        _date(data["createdAt"])
        for field in ("startedAt", "finishedAt", "recoveredAt"):
            if data[field] is not None:
                _date(data[field])
        if (state in TERMINAL) != (data["finishedAt"] is not None):
            raise ValueError("Invalid completion timestamp")
        if data["exitCode"] is not None and (type(data["exitCode"]) is not int or not 0 <= data["exitCode"] <= 0xFFFFFFFF):
            raise ValueError("Invalid exit code")
        if state == "succeeded" and (data["exitCode"] != 0 or data["error"] is not None):
            raise ValueError("Invalid successful result")
        if data["error"] is not None and (not isinstance(data["error"], dict)
                or not isinstance(data["error"].get("code"), str) or not isinstance(data["error"].get("message"), str)):
            raise ValueError("Invalid error")
        for field in ("logAvailable", "artifactsExpired"):
            if type(data[field]) is not bool:
                raise ValueError("Invalid artifact flag")
        if data["artifactsExpired"] and (not data["terminal"] or data["logAvailable"]):
            raise ValueError("Invalid expired artifacts state")
        if data["logAvailable"] and not self.path(identifier, "output.log").is_file():
            raise ValueError("A published journal is missing")
        if not isinstance(data["outputs"], list) or not isinstance(data["outputFiles"], list):
            raise ValueError("Invalid output list")
        from file_updates import validate_relative_path
        for relative in data["outputFiles"]:
            validate_relative_path(relative)
        for item in data["outputs"]:
            validate_relative_path(item["path"])
            if item["path"] not in data["outputFiles"] or type(item["size"]) is not int or item["size"] < 0:
                raise ValueError("Invalid output record")
            if type(item["available"]) is not bool or not isinstance(item["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", item["sha256"]):
                raise ValueError("Invalid output metadata")
            if data["artifactsExpired"] and item["available"]:
                raise ValueError("An expired output is still available")
            if item["available"] and not self.path(identifier, "outputs", *item["path"].split("/")).is_file():
                raise ValueError("A published output is missing")
        return data

    def _read(self, identifier):
        folder = self.path(identifier)
        if not folder.exists():
            if identifier in self._known:
                raise ValueError("A registered operation directory disappeared")
            raise ApiError(404, "UnknownOperation", "Unknown requestId.")
        return self._validate(identifier, self._json(folder / "status.json"))

    def _scan(self):
        records, identifiers = [], set()
        for folder in checked_path(self.workspace, self.operations).iterdir():
            checked_path(self.workspace, folder)
            identifier = request_uuid(folder.name)
            if folder.name != identifier or not folder.is_dir():
                raise ValueError("Invalid operation directory")
            identifiers.add(identifier)
            records.append(self._read(identifier))
        if self._known - identifiers:
            raise ValueError("Registered operations disappeared")
        self._known.update(identifiers)
        return records

    def check_modifiable(self):
        with self._io():
            self._ready()
            if not self.accepting_changes:
                raise ApiError(503, "ExecutorStopped", "Executor is shutting down.")
            self._scan()

    def stop_changes(self):
        with self.mutex:
            self.accepting_changes = False

    def read(self, identifier):
        identifier = request_uuid(identifier)
        with self._io():
            self._ready()
            self._scan()
            record = self._read(identifier)
            if self._expired(record):
                self._expire(record)
                record = self._read(identifier)
            return record

    def find(self, identifier):
        try:
            return self.read(identifier)
        except ApiError as exc:
            if exc.code == "UnknownOperation":
                return None
            raise

    def register(self, identifier, project_id, command, output_files=()):
        identifier = request_uuid(identifier)
        with self._io():
            self._ready()
            if self.find(identifier) is not None:
                raise ValueError("Operation already exists")
            folder = self.path(identifier)
            folder.mkdir(exist_ok=False)
            self._known.add(identifier)
            (folder / "outputs").mkdir()
            record = dict(schemaVersion=1, requestId=identifier, projectId=project_id,
                command=command, fingerprint=fingerprint(project_id, command), state="starting", terminal=False,
                createdAt=now(), startedAt=None, finishedAt=None, recoveredAt=None, pid=None,
                jobName=self.job_name(identifier), exitCode=None, error=None, logAvailable=False,
                artifactsExpired=False, outputFiles=list(output_files), outputs=[])
            self._validate(identifier, record)
            self._atomic(folder / "status.json", record)
            return record

    def _update(self, identifier, changes):
        record = self._read(identifier)
        if record["terminal"]:
            raise ValueError("A completed result cannot change")
        following = changes.get("state", record["state"])
        if following in ACTIVE and following != record["state"] and (record["state"], following) not in {
                ("starting", "running"), ("running", "finalizing")}:
            raise ValueError("Invalid state transition")
        allowed = {"state", "terminal", "startedAt", "finishedAt", "recoveredAt", "pid", "exitCode", "error", "logAvailable", "outputs"}
        if changes.keys() - allowed:
            raise ValueError("Unexpected status fields")
        record.update(changes)
        self._validate(identifier, record)
        self._atomic(self.path(identifier, "status.json"), record)
        return record

    def update(self, identifier, **changes):
        with self._io():
            self._ready()
            return self._update(request_uuid(identifier), changes)

    def read_log(self, identifier):
        # Do not hold the status mutex while reading a potentially large journal.
        record = self.read(identifier)
        if record["artifactsExpired"]:
            raise ApiError(410, "ArtifactsExpired", "Operation artifacts have expired.")
        if not record["logAvailable"]:
            return ""
        path = self.path(identifier, "output.log")
        try:
            decoder = codecs.getincrementaldecoder("utf-8")()
            return decoder.decode(path.read_bytes(), final=record["terminal"] and record["state"] != "interrupted")
        except FileNotFoundError:
            if not record["logAvailable"] and not record["terminal"]:
                return ""
            if self.read(identifier)["artifactsExpired"]:
                raise ApiError(410, "ArtifactsExpired", "Operation artifacts have expired.") from None
            raise self.unavailable("Operation journal is missing") from None
        except (OSError, UnicodeError) as exc:
            raise self.unavailable(str(exc)) from exc

    def output_path(self, identifier, relative):
        record = self.read(identifier)
        item = next((item for item in record["outputs"] if item["path"] == relative), None)
        if item is None:
            raise ApiError(404, "UnknownOutput", "Unknown output file.")
        if not item["available"]:
            raise ApiError(410, "ArtifactsExpired", "Operation artifacts have expired.")
        return self.path(identifier, "outputs", *relative.split("/"))

    def log_tail(self, identifier, record, limit=64 * 1024):
        """Read at most limit bytes, without splitting leading/trailing UTF-8 characters."""
        if not record["logAvailable"] or record["artifactsExpired"]:
            return "", False
        with self._io():
            self._ready()
            with self.path(identifier, "output.log").open("rb") as stream:
                end = os.fstat(stream.fileno()).st_size
                start = max(0, end - limit)
                stream.seek(start)
                data = stream.read(end - start)
            if start:
                while data and data[0] & 0xC0 == 0x80:
                    data = data[1:]
            decoder = codecs.getincrementaldecoder("utf-8")()
            return decoder.decode(data, final=record["terminal"] and record["state"] != "interrupted"), start > 0

    def open_artifact(self, identifier, relative=None):
        from artifact_stream import ArtifactStream
        identifier = request_uuid(identifier)
        with self._io():
            self._ready()
            record = self.read(identifier)
            if record["artifactsExpired"]:
                raise ApiError(410, "ArtifactsExpired", "Operation artifacts have expired.")
            if relative is None:
                if not record["logAvailable"]:
                    return None
                path = self.path(identifier, "output.log")
            else:
                path = self.output_path(identifier, relative)
            stream = path.open("rb")
            try:
                reader = ArtifactStream(self, identifier, stream, text=relative is None)
            except BaseException:
                stream.close()
                raise
            self._readers.add(reader)
            return reader

    def release_stream(self, reader):
        with self.mutex:
            self._readers.discard(reader)
            if self.closed or self.problem or any(item.identifier == reader.identifier for item in self._readers):
                return
            try:
                with self._io():
                    record = self._read(reader.identifier)
                    if record["artifactsExpired"]:
                        self._expire(record)
            except ApiError:
                # The store is fail-closed; do not throw from a WSGI close callback.
                pass

    def _expired(self, record):
        return record["terminal"] and _date(record["finishedAt"]) + timedelta(days=self.settings.retention_days) <= datetime.now(timezone.utc)

    def _expire(self, record):
        identifier = record["requestId"]
        if not record["artifactsExpired"]:
            record.update(artifactsExpired=True, logAvailable=False)
            for item in record["outputs"]:
                item["available"] = False
            self._atomic(self.path(identifier, "status.json"), record)
        if any(reader.identifier == identifier for reader in self._readers):
            return  # Existing downloads may finish; new downloads already receive 410.
        # Collect and validate the entire deletion set before deleting anything.
        files, directories = [], []
        def collect(folder):
            for child in checked_path(self.workspace, folder).iterdir():
                checked_path(self.workspace, child)
                child.relative_to(self.path(identifier, "outputs"))
                if child.is_dir():
                    collect(child)
                    directories.append(child)
                else:
                    files.append(child)
        outputs = self.path(identifier, "outputs")
        if outputs.exists():
            collect(outputs)
        journal = self.path(identifier, "output.log")
        if journal.exists():
            files.append(journal)
        for path in files:
            checked_path(self.workspace, path).unlink()
        for path in directories:
            checked_path(self.workspace, path).rmdir()

    def _cleanup(self):
        for record in self._scan():
            if self._expired(record):
                self._expire(record)

    def _purge_pending(self):
        pending = checked_path(self.workspace, self.root / "expired-operations")
        if not pending.exists():
            return
        if not pending.is_dir():
            raise ValueError("Invalid expired operations directory")
        for folder in pending.iterdir():
            checked_path(self.workspace, folder)
            if folder.name != request_uuid(folder.name) or not folder.is_dir():
                raise ValueError("Invalid expired operation directory")
            files, directories = [], []
            def collect(directory):
                for child in checked_path(self.workspace, directory).iterdir():
                    checked_path(self.workspace, child)
                    if child.is_dir():
                        collect(child)
                        directories.append(child)
                    elif child.is_file():
                        files.append(child)
                    else:
                        raise ValueError("Invalid expired operation entry")
            collect(folder)
            for path in files:
                checked_path(self.workspace, path).unlink()
            for path in directories:
                checked_path(self.workspace, path).rmdir()
            checked_path(self.workspace, folder).rmdir()
        pending.rmdir()

    def _remove_expired_on_start(self):
        for record in self._scan():
            if _date(record["createdAt"]) + timedelta(days=self.settings.retention_days) > datetime.now(timezone.utc):
                continue
            identifier = record["requestId"]
            pending = checked_path(self.workspace, self.root / "expired-operations")
            pending.mkdir(exist_ok=True)
            destination = checked_path(self.workspace, pending / identifier)
            if destination.exists():
                raise ValueError("Duplicate expired operation directory")
            self.path(identifier).rename(destination)
            self._known.discard(identifier)
        self._purge_pending()

    def cleanup(self):
        with self._io():
            self._ready()
            self._cleanup()

    def close(self):
        with self.mutex:
            self.closed = True
            for reader in tuple(self._readers):
                reader.close()
            if self.owner is not None:
                self.owner.close()
