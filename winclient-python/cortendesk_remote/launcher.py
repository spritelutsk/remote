"""Запуск окна сеанса дочерним процессом.

Команда собирается по-разному для замороженной сборки и для запуска из
исходников: у PyInstaller sys.executable — это уже само приложение, и
подкладывать ему путь к скрипту нельзя.
"""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path


def _command(url: str, title: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--session", url, title]

    # -m нужен, чтобы внутри дочернего процесса работали относительные импорты.
    return [sys.executable, "-m", "cortendesk_remote", "--session", url, title]


def open_session(url: str, title: str) -> tuple[bool, str]:
    """Возвращает (запущено ли встроенное окно, текст для статус-строки)."""
    try:
        import webview  # noqa: F401
    except ImportError:
        # Без pywebview сеанс всё равно можно открыть — в системном браузере.
        webbrowser.open(url)
        return False, "pywebview не установлен — сеанс открыт в браузере."

    cwd = str(Path(__file__).resolve().parent.parent)

    subprocess.Popen(  # noqa: S603 - команда собрана нами, пользовательский ввод только в url
        _command(url, title),
        cwd=None if getattr(sys, "frozen", False) else cwd,
    )

    return True, ""
