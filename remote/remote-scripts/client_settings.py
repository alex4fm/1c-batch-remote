"""Настройки подключения к ретранслятору."""
import os
from pathlib import Path
from urllib.parse import urlsplit

CONFIG = Path.cwd().resolve() / "remote"


def read_settings():
    try:
        lines = CONFIG.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        raise ValueError("Файл remote в корне проекта отсутствует") from None
    except UnicodeError:
        raise ValueError("Файл remote должен быть в кодировке UTF-8") from None
    settings = {}
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in {"token", "url"} or key in settings or not value:
            raise ValueError(f"Некорректная настройка в remote, строка {number}")
        settings[key] = value
    return settings


def access_token():
    settings = read_settings()
    token = os.environ.get("BATCH_EXECUTOR_TOKEN") or settings.get("token")
    if not token or not token.strip():
        raise ValueError("В remote необходимо указать непустой token")
    return token


def api_url():
    url = read_settings().get("url")
    if not url:
        raise ValueError("В remote необходимо указать url удалённого API")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("Некорректный url в remote") from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or (port is not None and port == 0)):
        raise ValueError("url в remote должен содержать только схему, адрес сервера и порт")
    return url.rstrip("/") + "/batch/v1"
