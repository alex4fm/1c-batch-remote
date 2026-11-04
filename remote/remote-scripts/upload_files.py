#!/usr/bin/env python3
"""Передача файлов относительно корня проекта исполнителя; результат выводится в JSON."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from client_settings import access_token, api_url

ROOT = Path.cwd().resolve()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description="Передать файлы из локального проекта в корень исполнителя")
    parser.add_argument("files", nargs="+", help="Пути к файлам в проекте")
    parser.add_argument("--timeout", type=float, default=180, help="Лимит ожидания результата, секунд")
    args = parser.parse_args()
    token = access_token()
    if args.timeout <= 0:
        raise ValueError("Тайм-аут должен быть положительным")
    base = api_url().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Некорректный адрес API")
    origin = (parsed.scheme, parsed.netloc)
    request_id = str(uuid.uuid4())
    records, contents, seen = [], [], set()
    for name in args.files:
        path = Path(name)
        path = (path if path.is_absolute() else ROOT / path).resolve(strict=True)
        try:
            relative = path.relative_to(ROOT).as_posix()
        except ValueError:
            raise ValueError("Файл находится вне корня проекта: " + name) from None
        if relative.casefold() in {"remote", "src/export-scripts/settings.json", "src/remote-scripts/settings.json"}:
            raise ValueError("Файл настроек подключения нельзя передавать")
        if not path.is_file() or relative.casefold() in seen:
            raise ValueError("Не файл или повтор пути: " + name)
        seen.add(relative.casefold())
        data = path.read_bytes()
        records.append({"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        contents.append(data)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"requestId": request_id, "files": records}, ensure_ascii=False).encode("utf-8"))
        for record, data in zip(records, contents):
            z.writestr("payload/" + record["path"], data)
    print(json.dumps({"event": "upload_started", "requestId": request_id}, ensure_ascii=False), file=sys.stderr, flush=True)
    deadline = time.monotonic() + args.timeout
    opener = urllib.request.build_opener(NoRedirect())

    def request(url, data=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Истёк срок ожидания; результат передачи неизвестен")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/zip"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with opener.open(req, timeout=remaining) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from None

    code, result = request(base + "/files", archive.getvalue())
    while code == 202 or (isinstance(result, dict) and result.get("terminal") is False):
        status_url = result.get("statusUrl")
        if not isinstance(status_url, str):
            raise ValueError("Сервер не вернул адрес статуса")
        status_url = urllib.parse.urljoin(base + "/", status_url)
        target = urllib.parse.urlsplit(status_url)
        if (target.scheme, target.netloc) != origin or target.username or target.password:
            raise ValueError("Адрес статуса принадлежит другому серверу")
        delay = result.get("pollAfterSeconds", 2)
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) or not 0 < delay <= 60:
            raise ValueError("Некорректный интервал опроса")
        time.sleep(min(delay, max(0, deadline - time.monotonic())))
        code, result = request(status_url)
    if not isinstance(result, dict) or result.get("requestId") != request_id or result.get("error") is not None:
        raise ValueError("Сервер вернул ошибку или другой requestId: " + json.dumps(result, ensure_ascii=False))
    if result.get("terminal") is True and result.get("state") != "succeeded":
        raise ValueError("Операция не завершилась успешно: " + json.dumps(result, ensure_ascii=False))
    confirmed = result.get("files")
    if not isinstance(confirmed, list) or len(confirmed) != len(records) or sorted(confirmed, key=lambda x: x.get("path", "")) != sorted(records, key=lambda x: x["path"]):
        raise ValueError("Подтверждение файлов не совпадает с отправленным пакетом: " + json.dumps(result, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
