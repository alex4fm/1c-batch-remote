"""Authenticated HTTP server for health checks and validated source uploads."""

import argparse
import hmac
import logging
import os
from pathlib import Path
import signal
import socket
import struct

from flask import Flask, jsonify, request
from waitress import serve
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from executor_config import ApiError, access_token, load_settings, read_settings_json
from file_updates import FileUpdater
from operation_service import OperationService
from http_operations import register_operation_routes
from python_version import notify_python_version


def create_app(token: str, settings=None) -> Flask:
    if not isinstance(token, str) or not token:
        raise ValueError("Токен доступа не задан.")
    settings = settings or load_settings()
    executor = OperationService(settings)
    updater = FileUpdater(settings, busy=executor.busy, storage_guard=executor.store.check_modifiable)
    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False
    app.config["MAX_CONTENT_LENGTH"] = settings.limits.max_zip_bytes
    app.extensions["file_updater"] = updater
    app.extensions["process_executor"] = executor
    app.extensions["operation_store"] = executor.store

    @app.errorhandler(ApiError)
    def api_error(error):
        return jsonify(error.body()), error.status

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return api_error(ApiError(413, "UploadTooLarge", "ZIP size limit exceeded."))

    @app.errorhandler(HTTPException)
    def http_error(error):
        return api_error(ApiError(error.code, error.name.replace(" ", ""), error.description))

    @app.errorhandler(Exception)
    def unexpected_error(error):
        app.logger.exception("Unhandled executor error")
        return api_error(ApiError(500, "InternalError", "Unexpected executor error."))

    @app.before_request
    def authenticate():
        expected = f"Bearer {token}".encode("utf-8")
        supplied = request.headers.get("Authorization", "").encode("utf-8")
        if not hmac.compare_digest(supplied, expected):
            response = jsonify(error={
                "code": "Unauthorized",
                "message": "Bearer token is missing or invalid.",
                "details": None,
            })
            response.status_code = 401
            response.headers["WWW-Authenticate"] = "Bearer"
            return response

    @app.after_request
    def disable_cache(response):
        response.headers["Cache-Control"] = "no-store"
        if response.is_json:
            response.content_type = "application/json; charset=utf-8"
        return response

    @app.get("/health")
    def health():
        executor.store.check_modifiable()
        return jsonify(
            service="1c-batch-python-executor",
            stage="01.1",
            status="ok",
            message="Окружение Python готово",
            error=None,
        )

    @app.post("/v1/files")
    def update_files():
        if request.mimetype != "application/zip":
            raise ApiError(400, "InvalidContentType", "Use Content-Type: application/zip.")
        return jsonify(updater.update(request.stream))

    register_operation_routes(app, executor)

    # Apache removes /batch; direct connections use the same public URLs.
    for rule in list(app.url_map.iter_rules()):
        app.add_url_rule("/batch" + rule.rule, endpoint="batch_" + rule.endpoint,
                         view_func=app.view_functions[rule.endpoint], methods=rule.methods)

    return app


def stop_server(_signum, _frame):
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    parser.add_argument("--config", type=Path, help="Local settings JSON (default: ../settings.json relative to server.py)")
    args = parser.parse_args()
    notify_python_version("сервер")
    if struct.calcsize("P") != 8:
        parser.error("64-bit Python is required.")
    signal.signal(signal.SIGTERM, stop_server)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop_server)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    app = None
    try:
        token = access_token(args.config)
        raw = read_settings_json(args.config)
        port = args.port if args.port is not None else raw.get("port", 8765)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535.")
        settings = load_settings(args.config)
        app = create_app(token, settings)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            # Windows SO_REUSEADDR otherwise permits competing listeners.
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("0.0.0.0", port))
            logging.info("Executor PID: %s; stop with Ctrl+C.", os.getpid())
            serve(app, sockets=[listener], threads=4,
                  max_request_body_size=settings.limits.max_zip_bytes)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        logging.error("Cannot start server: %s", exc)
        return 1
    except (ValueError, ApiError) as exc:
        logging.error("Invalid executor settings: %s", exc)
        return 1
    finally:
        if app is not None:
            app.extensions["process_executor"].close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
