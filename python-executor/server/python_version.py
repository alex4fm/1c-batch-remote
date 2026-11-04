"""Advisory Python version check; patch versions do not affect startup."""

from pathlib import Path
import sys


def notify_python_version(context: str) -> None:
    try:
        parts = Path(__file__).with_name(".python-version").read_text(encoding="utf-8").strip().split(".")
        if len(parts) < 2:
            raise ValueError("Major and minor version are required")
        recommended = tuple(map(int, parts[:2]))
    except (OSError, ValueError):
        print("Уведомление: не удалось прочитать версию из .python-version. Запуск продолжается.",
              file=sys.stderr, flush=True)
        return

    current = sys.version_info[:2]
    if current != recommended:
        expected = ".".join(map(str, recommended))
        actual = ".".join(map(str, current))
        print(f"Уведомление ({context}): рекомендуется Python {expected}, используется {actual}. "
              "Запуск продолжается.", file=sys.stderr, flush=True)
