"""Окно сеанса: нативный веб-клиент CortenDesk во встроенном браузере.

Почему отдельным процессом. И Tkinter, и pywebview требуют главного потока
процесса: pywebview.start() запускает свой цикл событий и не возвращает
управление, пока окно не закроют. Держать их в одном процессе нельзя, поэтому
список устройств запускает сеанс через subprocess — заодно падение окна сеанса
не уносит с собой всё приложение.

Разбор навигации повторяет логику WPF- и Electron-версий. Порядок ветвлений
важен: сначала успех, потом чужой хост (портал-провайдер SSO — туда лезть
нельзя), потом формы входа консоли, и только оставшееся считается «вошли, но
не туда» — это редирект на главную после логина, откуда мы возвращаемся сами.
"""

from __future__ import annotations

import sys
import urllib.parse

from . import store

#: Сколько раз возвращаться на /webclient, если сервер увёл на другую страницу.
MAX_RESUME_ATTEMPTS = 3

_AUTH_PREFIXES = ("/login", "/oidc", "/invite", "/forgot-password", "/reset-password")


def is_auth_page(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.startswith(prefix) for prefix in _AUTH_PREFIXES)


def storage_path() -> str:
    """Профиль встроенного браузера: тут копится cookie сессии консоли."""
    return str(store.app_dir() / "webview")


def run(url: str, title: str) -> int:
    """Точка входа дочернего процесса. Возвращает код выхода."""
    try:
        import webview
    except ImportError:
        print(
            "Не установлен pywebview — окно сеанса открыть нечем.\n"
            "Установите его командой:  pip install pywebview",
            file=sys.stderr,
        )
        return 2

    console_host = urllib.parse.urlsplit(url).netloc
    state = {"attempts": 0}

    window = webview.create_window(title, url, width=1280, height=820, text_select=True)

    def on_loaded() -> None:
        current = window.get_current_url()
        if not current:
            return

        parts = urllib.parse.urlsplit(current)

        if parts.path.lower() == "/webclient":
            state["attempts"] = 0
            return

        if parts.netloc != console_host:
            return  # внешний провайдер SSO — не мешаем

        if is_auth_page(parts.path):
            return  # форма входа — ждём пользователя

        if state["attempts"] < MAX_RESUME_ATTEMPTS:
            state["attempts"] += 1
            window.load_url(url)

    window.events.loaded += on_loaded

    # private_mode=False со своим storage_path — именно это сохраняет cookie
    # консоли между запусками, иначе вход спрашивался бы каждый раз.
    webview.start(private_mode=False, storage_path=storage_path())
    return 0
