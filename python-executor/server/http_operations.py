"""Public HTTP contract; private runtime metadata never enters operation responses."""

import json
from urllib.parse import quote

from flask import Response, jsonify, request

from executor_config import ApiError
from file_updates import unique_json
from operation_store import request_uuid


EXECUTE_BYTES = 128 * 1024
START_WAIT_SECONDS = 25


def status_url(identifier):
    return "/batch/v1/operations/" + identifier


def public_record(store, identifier):
    with store.mutex:
        record = store.read(identifier)
        log, truncated = store.log_tail(identifier, record)
    base = status_url(record["requestId"])
    fields = ("requestId", "state", "terminal", "createdAt", "startedAt", "finishedAt")
    result = {key: record[key] for key in fields}
    result.update(statusUrl=base, log=log, logTruncated=truncated,
                  logAvailable=record["logAvailable"], logUrl=base + "/log", outputs=[], error=None)
    if record["error"] is not None:
        result["error"] = {**record["error"], "details": record["error"].get("details")}
    if record["exitCode"] is not None:
        result["exitCode"] = record["exitCode"]
    if not record["terminal"]:
        result["pollAfterSeconds"] = 2
    for item in record["outputs"]:
        result["outputs"].append({**item, "url": base + "/outputs/" + quote(item["path"], safe="/")})
    return result


def execute_body():
    if request.mimetype != "application/json":
        raise ApiError(400, "InvalidContentType", "Use Content-Type: application/json.")
    charset = request.mimetype_params.get("charset", "utf-8").lower().replace("_", "-")
    if charset not in ("utf-8", "utf8"):
        raise ApiError(400, "InvalidContentType", "Execute JSON must use UTF-8.")
    if request.content_length is not None and request.content_length > EXECUTE_BYTES:
        raise ApiError(413, "RequestTooLarge", "Execute JSON must not exceed 128 KiB.")
    raw = request.stream.read(EXECUTE_BYTES + 1)
    if len(raw) > EXECUTE_BYTES:
        raise ApiError(413, "RequestTooLarge", "Execute JSON must not exceed 128 KiB.")
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_json,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, ValueError, RecursionError):
        raise ApiError(400, "InvalidRequest", "Execute body must contain valid UTF-8 JSON.") from None
    if not isinstance(body, dict) or set(body) != {"requestId", "command"}:
        raise ApiError(400, "InvalidRequest", "Exactly requestId and command are required.")
    command = body["command"]
    if not isinstance(command, str) or not command.strip() or "\0" in command or len(command) > 30000:
        raise ApiError(400, "InvalidCommand", "command must be a non-empty string without NUL, at most 30000 characters.")
    body["requestId"] = request_uuid(body["requestId"])
    return body


def register_operation_routes(app, executor):
    store = executor.store

    @app.post("/v1/execute")
    def execute():
        body = execute_body()
        identifier = body["requestId"]
        try:
            try:
                # Keep the v1 on-disk discriminator stable for existing UUID records.
                # It no longer selects a project or appears in the public API.
                operation, created = executor.submit("executor-probe", body["command"], identifier)
            except ApiError:
                raise
            except Exception as exc:
                saved = store.find(identifier)
                if saved is not None and saved["state"] == "failed":
                    return jsonify(public_record(store, identifier)), 500
                raise ApiError(500, "ExecutionFailed", "Could not start execution.") from exc
            record = operation.await_started(START_WAIT_SECONDS)
            payload = public_record(store, identifier)
            code = 200 if payload["terminal"] else 202
            if created and record["state"] == "failed" and record["startedAt"] is None:
                code = 500
            response = jsonify(payload)
            response.status_code = code
            if code == 202:
                response.headers["Location"] = payload["statusUrl"]
            return response
        except ApiError as exc:
            exc.request_id = identifier
            if exc.code == "StartupPending":
                exc.details = {"statusUrl": status_url(identifier)}
            raise

    @app.get("/v1/operations/<identifier>")
    def operation_status(identifier):
        return jsonify(public_record(store, request_uuid(identifier)))

    def artifact_response(identifier, relative=None):
        reader = store.open_artifact(identifier, relative)
        response = Response(reader if reader is not None else b"",
                            content_type="text/plain; charset=utf-8" if relative is None else "application/octet-stream")
        response.content_length = reader.size if reader is not None else 0
        if reader is not None:
            response.call_on_close(reader.close)
        if relative is not None:
            response.headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(relative.rsplit("/", 1)[-1], safe="")
        return response

    @app.get("/v1/operations/<identifier>/log")
    def operation_log(identifier):
        return artifact_response(request_uuid(identifier))

    @app.get("/v1/operations/<identifier>/outputs/<path:file_path>")
    def operation_output(identifier, file_path):
        return artifact_response(request_uuid(identifier), file_path)
