#!/usr/bin/env python3
"""Отправка команды исполнителю и ожидание результата."""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from client_settings import access_token, api_url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description="Отправить команду исполнителю")
    parser.add_argument("command", nargs="?", help="Команда для исполнителя одной строкой")
    parser.add_argument("--resume", help="Продолжить опрос по сохранённому UUID")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    token = access_token()
    if args.timeout <= 0:
        raise ValueError("Тайм-аут должен быть положительным")
    base = api_url().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Некорректный адрес API")
    if args.resume:
        if args.command is not None:
            raise ValueError("При --resume не указывайте команду")
        request_id = str(uuid.UUID(args.resume))
        command = None
    else:
        if not args.command or not args.command.strip():
            raise ValueError("Укажите команду одной строкой")
        command = args.command
        request_id = str(uuid.uuid4())
    print(json.dumps({"event": "operation", "requestId": request_id}, ensure_ascii=False), file=sys.stderr, flush=True)
    deadline = time.monotonic() + args.timeout
    opener = urllib.request.build_opener(NoRedirect())

    def request(method, suffix, body=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Истёк срок ожидания; продолжите опрос с --resume и прежним UUID")
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + suffix, data=body, headers=headers, method=method)
        try:
            with opener.open(req, timeout=min(remaining, 190)) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    path = "/operations/" + request_id
    if command is None:
        code, raw = request("GET", path)
    else:
        body = json.dumps({"requestId": request_id, "command": command}, ensure_ascii=False).encode("utf-8")
        try:
            code, raw = request("POST", "/execute", body)
        except (OSError, TimeoutError):
            # A lost response does not imply that the command was not started.
            code, raw = request("GET", path)
            if code == 404:
                code, raw = request("POST", "/execute", body)
    while True:
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise ValueError(f"HTTP {code}: ответ не является JSON") from None
        if code not in (200, 202) or not isinstance(result, dict):
            raise RuntimeError(f"HTTP {code}: " + json.dumps(result, ensure_ascii=False))
        if result.get("requestId") != request_id or type(result.get("terminal")) is not bool:
            raise ValueError("Некорректный ответ статуса операции")
        if result["terminal"]:
            break
        delay = result.get("pollAfterSeconds", 2)
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) or not 0 < delay <= 60:
            raise ValueError("Некорректный интервал опроса")
        time.sleep(min(delay, max(0, deadline - time.monotonic())))
        code, raw = request("GET", path)
    if result.get("logTruncated"):
        code, raw = request("GET", path + "/log")
        if code != 200:
            raise RuntimeError(f"Не удалось получить полный журнал: HTTP {code}")
        result["log"] = raw.decode("utf-8", errors="replace")
        result["logTruncated"] = False
    # The executor may decode UTF-8 BAT output as CP866. Recover only lines
    # that are valid UTF-8 after reversing that exact, lossless conversion.
    if isinstance(result.get("log"), str):
        def repair(line):
            try:
                return line.encode("cp866").decode("utf-8")
            except (UnicodeError, LookupError):
                return line
        result["log"] = ''.join(map(repair, result["log"].splitlines(keepends=True)))
    print(json.dumps(result, ensure_ascii=False))
    if result.get("state") != "succeeded" or result.get("exitCode") != 0 or result.get("error") is not None:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
