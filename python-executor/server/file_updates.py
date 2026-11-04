"""Validated ZIP uploads. No archive extraction to project paths and no command execution."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import zlib
from uuid import UUID
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED, ZIP_STORED

from executor_config import ApiError, checked_path


CHUNK = 64 * 1024
RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$"}
RESERVED.update(f"{prefix}{number}" for prefix in ("com", "lpt") for number in "123456789¹²³")


def validate_relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise ApiError(403, "ForbiddenPath", "A relative path using / is required.")
    parts = value.split("/")
    for part in parts:
        if (not part or part in (".", "..") or part.endswith((".", " "))
                or any(ord(c) < 32 or c in '<>:"|?*~' for c in part)
                or part.split(".")[0].casefold() in RESERVED):
            raise ApiError(403, "ForbiddenPath", "Unsafe Windows path.", {"path": value})
    return parts


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class FileUpdater:
    def __init__(self, settings, busy=None, storage_guard=None):
        self.settings = settings
        self.busy = busy if busy is not None else threading.Lock()
        self.storage_guard = storage_guard

    def _deadline(self, deadline):
        if time.monotonic() > deadline:
            raise ApiError(500, "UpdateTimedOut", "File update time limit exceeded.")

    def _destination(self, project, relative):
        parts = validate_relative_path(relative)
        if parts[0].casefold() in {"server", "scripts", "venv", "settings.json", ".1c-devbase.bat", "start-python_executor.cmd"}:
            raise ApiError(403, "ForbiddenPath", "Executor files and runtime cannot be uploaded.")
        checked_path(self.settings.workspace, project.root)
        if not project.root.is_dir():
            raise ApiError(503, "ProjectUnavailable", "Configured project directory is unavailable.")
        target = checked_path(project.root, project.root.joinpath(*parts))
        package = Path(__file__).resolve().parents[1]
        protected = (None if project.root.is_relative_to(package) else package,
                     self.settings.runtime, self.settings.staging)
        if any(path is not None and target.is_relative_to(path) for path in protected):
            raise ApiError(403, "ForbiddenPath", "Executor files and runtime cannot be uploaded.")
        for parent in target.parents:
            if parent == project.root:
                break
            if parent.exists() and not parent.is_dir():
                raise ApiError(400, "InvalidPath", "A parent path is not a directory.", {"path": relative})
        if target.exists() and not target.is_file():
            raise ApiError(400, "InvalidPath", "Destination is not a regular file.", {"path": relative})
        return target

    def _manifest(self, raw):
        try:
            manifest = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_json,
                                  parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (UnicodeError, ValueError, RecursionError):
            raise ApiError(400, "InvalidManifest", "Manifest must contain valid UTF-8 JSON.") from None
        if not isinstance(manifest, dict):
            raise ApiError(400, "InvalidManifest", "Manifest must be an object.")
        request_id = manifest.get("requestId")
        try:
            if not isinstance(request_id, str) or str(UUID(request_id)) != request_id.lower():
                raise ValueError
        except ValueError:
            raise ApiError(400, "InvalidManifest", "requestId must be a UUID.") from None
        if set(manifest) != {"requestId", "files"}:
            raise ApiError(400, "InvalidManifest", "Exactly requestId and files are required.")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ApiError(400, "InvalidManifest", "files must be a non-empty array.")
        return manifest

    def _archive_index(self, archive):
        infos = archive.infolist()
        if len(infos) > self.settings.limits.max_files * 4 + 128:
            raise ApiError(413, "UploadTooLarge", "Too many ZIP entries.")
        entries, directories, folded = {}, set(), set()
        total = 0
        for info in infos:
            name = info.orig_filename
            if name != info.filename or "\x00" in name:
                raise ApiError(403, "ForbiddenPath", "NUL bytes in ZIP paths are forbidden.")
            relative = name[:-1] if info.is_dir() else name
            validate_relative_path(relative)
            key = relative.casefold()
            if key in folded:
                raise ApiError(400, "InvalidArchive", "Duplicate ZIP path.", {"path": relative})
            folded.add(key)
            mode = stat.S_IFMT(info.external_attr >> 16)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or info.external_attr & 0x400:
                raise ApiError(403, "ForbiddenPath", "ZIP links and special files are forbidden.")
            if info.flag_bits & 1 or info.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                raise ApiError(400, "InvalidArchive", "Use an unencrypted stored or deflated ZIP.")
            if info.is_dir():
                if info.file_size:
                    raise ApiError(400, "InvalidArchive", "ZIP directory contains data.")
                directories.add(relative)
            else:
                if mode == stat.S_IFDIR:
                    raise ApiError(400, "InvalidArchive", "Invalid ZIP file type.")
                entries[name] = info
                total += info.file_size
        if total > self.settings.limits.max_unpacked_bytes:
            raise ApiError(413, "UploadTooLarge", "Unpacked size limit exceeded.")
        if "manifest.json" not in entries:
            raise ApiError(400, "InvalidArchive", "manifest.json is missing.")
        return entries, directories

    def _read_member(self, archive, info, destination, remaining, deadline):
        size, digest = 0, hashlib.sha256()
        with archive.open(info) as stream:
            while True:
                self._deadline(deadline)
                block = stream.read(min(CHUNK, remaining - size + 1))
                if not block:
                    break
                size += len(block)
                if size > remaining:
                    raise ApiError(413, "UploadTooLarge", "Actual unpacked size limit exceeded.")
                destination.write(block)
                digest.update(block)
        return size, digest.hexdigest()

    def _stage(self, archive, folder, deadline, context):
        entries, directories = self._archive_index(archive)
        manifest_buffer = io.BytesIO()
        size, _ = self._read_member(archive, entries["manifest.json"], manifest_buffer,
                                    self.settings.limits.max_unpacked_bytes, deadline)
        manifest = self._manifest(manifest_buffer.getvalue())
        context.update(requestId=manifest["requestId"])
        project = self.settings.project
        if len(manifest["files"]) > self.settings.limits.max_files:
            raise ApiError(413, "UploadTooLarge", "File count limit exceeded.")
        records, keys = [], set()
        for number, item in enumerate(manifest["files"]):
            if not isinstance(item, dict):
                raise ApiError(400, "InvalidManifest", "Each file must be an object.")
            relative, length, digest = item.get("path"), item.get("size"), item.get("sha256")
            validate_relative_path(relative)
            key = relative.casefold()
            if key in keys:
                raise ApiError(400, "InvalidManifest", "Duplicate manifest path.")
            keys.add(key)
            if type(length) is not int or length < 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise ApiError(400, "InvalidManifest", "Invalid file size or SHA-256.")
            self._destination(project, relative)
            records.append({"path": relative, "size": length, "sha256": digest.lower(),
                            "staged": folder / f"{number}.bin"})
        for record in records:
            parts = record["path"].casefold().split("/")
            if any("/".join(parts[:i]) in keys for i in range(1, len(parts))):
                raise ApiError(400, "InvalidManifest", "A file is also used as a directory.")
        expected = {"manifest.json", *("payload/" + record["path"] for record in records)}
        if set(entries) != expected:
            raise ApiError(400, "InvalidArchive", "ZIP files do not match the manifest.")
        valid_directories = {name[:i] for name in expected for i, char in enumerate(name) if char == "/"}
        if directories - valid_directories:
            raise ApiError(400, "InvalidArchive", "ZIP contains unrelated directories.")
        for record in records:
            info = entries["payload/" + record["path"]]
            if info.file_size != record["size"]:
                raise ApiError(400, "SizeMismatch", "ZIP size differs from manifest.", {"path": record["path"]})
            with record["staged"].open("xb") as output:
                count, digest = self._read_member(archive, info, output,
                    self.settings.limits.max_unpacked_bytes - size, deadline)
            size += count
            if count != record["size"] or digest != record["sha256"]:
                raise ApiError(400, "ChecksumMismatch", "File checksum does not match.", {"path": record["path"]})
        return project, records

    def _commit(self, project, records, deadline, written):
        confirmed = []
        # Check every destination again before changing any project file.
        for record in records:
            self._destination(project, record["path"])
        for record in records:
            self._deadline(deadline)
            target = self._destination(project, record["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            self._destination(project, record["path"])
            descriptor, temporary = tempfile.mkstemp(prefix=".batch-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as output, record["staged"].open("rb") as source:
                    while block := source.read(CHUNK):
                        self._deadline(deadline)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                self._destination(project, record["path"])
                os.replace(temporary, target)
                written.append(record["path"])
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._destination(project, record["path"])
            digest, count = hashlib.sha256(), 0
            with target.open("rb") as result:
                while block := result.read(CHUNK):
                    self._deadline(deadline)
                    digest.update(block)
                    count += len(block)
            if count != record["size"] or digest.hexdigest() != record["sha256"]:
                raise ApiError(500, "WriteVerificationFailed", "Destination checksum verification failed.")
            self._deadline(deadline)
            confirmed.append({"path": record["path"], "size": count, "sha256": digest.hexdigest()})
        return confirmed

    def update(self, stream):
        if self.storage_guard is not None:
            self.storage_guard()
        if not self.busy.acquire(blocking=False):
            raise ApiError(409, "Busy", "Another modifying operation is in progress.")
        deadline = time.monotonic() + self.settings.limits.update_timeout_seconds
        written, context = [], {}
        try:
            if self.storage_guard is not None:
                self.storage_guard()
            staging = checked_path(self.settings.workspace, self.settings.staging)
            staging.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="upload-", dir=staging) as directory:
                folder = Path(directory)
                archive_path = folder / "request.zip"
                size = 0
                with archive_path.open("xb") as output:
                    while True:
                        self._deadline(deadline)
                        block = stream.read(min(CHUNK, self.settings.limits.max_zip_bytes - size + 1))
                        if not block:
                            break
                        size += len(block)
                        if size > self.settings.limits.max_zip_bytes:
                            raise ApiError(413, "UploadTooLarge", "ZIP size limit exceeded.")
                        output.write(block)
                try:
                    with ZipFile(archive_path) as archive:
                        project, records = self._stage(archive, folder, deadline, context)
                except (BadZipFile, NotImplementedError, RuntimeError, EOFError, UnicodeError, zlib.error) as exc:
                    raise ApiError(400, "InvalidArchive", "ZIP archive is invalid or unsupported.") from exc
                files = self._commit(project, records, deadline, written)
            return {**context, "files": files, "error": None}
        except OSError as exc:
            error = ApiError(500, "WriteFailed", "File update failed.", {"writtenFiles": written})
            error.request_id = context.get("requestId")
            raise error from exc
        except ApiError as exc:
            exc.request_id = context.get("requestId")
            exc.details = {**(exc.details or {}), "writtenFiles": written}
            raise
        finally:
            self.busy.release()
