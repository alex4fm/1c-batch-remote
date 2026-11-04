"""Local settings for an executor serving a single project."""

from dataclasses import dataclass, field
import codecs
import json
import os
from pathlib import Path
import re
import stat


class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details
        self.request_id = None

    def body(self):
        body = {"error": {"code": self.code, "message": str(self), "details": self.details}}
        if self.request_id is not None:
            body["requestId"] = self.request_id
        return body


def project_root():
    return Path(__file__).resolve().parents[1]


def checked_path(workspace, path):
    """Inspect components only after lexical containment; reject Windows reparse points."""
    workspace = Path(os.path.abspath(workspace))
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        raise ApiError(403, "ForbiddenPath", "Path is outside the project workspace.") from None
    current = workspace
    for component in (None, *relative.parts):
        if component is not None:
            current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ApiError(403, "ForbiddenPath", "Links and reparse points are not allowed.")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ApiError(403, "ForbiddenPath", "Hard-linked files are not allowed.")
    return path


@dataclass(frozen=True)
class Limits:
    max_zip_bytes: int = 32 * 1024 * 1024
    max_unpacked_bytes: int = 128 * 1024 * 1024
    max_files: int = 10_000
    update_timeout_seconds: float = 120
    execution_timeout_seconds: float = 600
    result_timeout_seconds: float = 30


@dataclass(frozen=True)
class Project:
    root: Path
    console_encoding: str = "cp866"
    # Legacy in-process snapshot callers; never loaded from settings.json.
    output_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    workspace: Path
    staging: Path
    project: Project
    limits: Limits = field(default_factory=Limits)
    runtime: Path | None = None
    retention_days: float = 7


def read_settings_json(path=None):
    workspace = project_root()
    path = checked_path(workspace, path or Path(__file__).resolve().parents[1] / "settings.json")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("Settings must be a JSON object.")
    return raw


def access_token(path=None):
    """Resolve the secret without including its value in settings objects or errors."""
    raw = read_settings_json(path)
    # The selected file is authoritative; an invalid value never falls back to env.
    token = raw["token"] if "token" in raw else os.environ.get("BATCH_EXECUTOR_TOKEN", "")
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-fA-F]{36}", token) is None:
        raise ValueError("token должен содержать ровно 36 шестнадцатеричных символов (0–9, a–f).")
    return token


def load_settings(path=None, *, server_dir=None):
    workspace = project_root()
    raw = read_settings_json(path)
    obsolete = {"host", "root", "projects", "allowedWriteDirs", "runtimeDir", "createIfMissing", "outputFiles"} & raw.keys()
    if obsolete:
        raise ValueError("Удалите устаревшие настройки: " + ", ".join(sorted(obsolete)))
    limits_raw = raw.get("limits", {})
    names = {"maxZipBytes": "max_zip_bytes", "maxUnpackedBytes": "max_unpacked_bytes",
             "maxFiles": "max_files", "updateTimeoutSeconds": "update_timeout_seconds",
             "executionTimeoutSeconds": "execution_timeout_seconds",
             "resultTimeoutSeconds": "result_timeout_seconds"}
    if not isinstance(limits_raw, dict) or set(limits_raw) - names.keys():
        raise ValueError("Unknown or invalid limits settings.")
    for key, value in limits_raw.items():
        expected_type = (int, float) if key.endswith("Seconds") else (int,)
        if type(value) not in expected_type or not 0 < value < float("inf"):
            raise ValueError(f"Invalid limit: {key}")
    limits = Limits(**{names[key]: value for key, value in limits_raw.items()})
    package = Path(server_dir).parent if server_dir is not None else Path(__file__).resolve().parents[1]
    root = checked_path(workspace, package)
    encoding = raw.get("consoleEncoding", "cp866")
    if not isinstance(encoding, str):
        raise ValueError("consoleEncoding must name a text encoding.")
    try:
        b"sample".decode(encoding, errors="replace")
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        if not isinstance(decoder.decode(b"", final=False), str):
            raise ValueError("A text decoder is required.")
    except (LookupError, ValueError, TypeError, UnicodeError) as exc:
        raise ValueError(f"Invalid consoleEncoding: {encoding}") from exc
    runtime = checked_path(workspace, Path(server_dir) / "runtime" if server_dir is not None else runtime_path())
    retention = raw.get("artifactRetentionDays", 7)
    if type(retention) not in (int, float) or not 0 < retention <= 365000:
        raise ValueError("artifactRetentionDays must be positive and at most 365000 days.")
    staging = checked_path(workspace, runtime / "uploads")
    return Settings(workspace, staging, Project(root, encoding), limits, runtime, retention)


def runtime_path():
    return Path(__file__).resolve().parent / "runtime"
