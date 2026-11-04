"""Prepare the local Python environment and start the diagnostic server."""

import os
from pathlib import Path
import struct
import subprocess
import sys
import venv

from python_version import notify_python_version
from executor_config import ApiError, access_token, project_root


def main() -> int:
    source = Path(__file__).resolve().parent
    project = project_root()
    notify_python_version("подготовка окружения")
    if struct.calcsize("P") != 8:
        print("Установите 64-разрядный Python по ссылке из инструкции.", file=sys.stderr)
        return 1

    token = access_token()

    environment = source.parent / "venv"
    interpreter = environment / "Scripts" / "python.exe"
    if not interpreter.exists():
        print("Создаю окружение Python…", flush=True)
        venv.create(environment, with_pip=True)

    subprocess.run(
        [str(interpreter), "-c",
         "import struct; assert struct.calcsize('P') == 8, '64-bit Python is required'"],
        check=True,
    )
    print("Проверяю и устанавливаю компоненты…", flush=True)
    subprocess.run(
        [str(interpreter), "-m", "pip", "install", "--disable-pip-version-check",
         "-r", str(source / "requirements.txt")],
        check=True,
    )
    subprocess.run([str(interpreter), "-m", "pip", "check"], check=True)

    server_environment = os.environ.copy()
    server_environment["BATCH_EXECUTOR_TOKEN"] = token
    print("Запускаю сервер. Для остановки нажмите Ctrl+C.", flush=True)
    return subprocess.run(
        [str(interpreter), "-u", str(source / "server.py")],
        cwd=project,
        env=server_environment,
    ).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except (OSError, ValueError, ApiError, subprocess.CalledProcessError) as exc:
        print(f"Подготовка или запуск не удались: {exc}", file=sys.stderr)
        raise SystemExit(1)
