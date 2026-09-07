"""Сетевые вызовы в фоновом потоке.

GTK, как и любой тулкит, не терпит обращений к виджетам из чужого потока.
Работа уходит в поток, результат возвращается через GLib.idle_add — то есть
уже в главном цикле GTK.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from gi.repository import GLib

from .api import ApiError


def run_async(work: Callable[[], Any], on_done: Callable[[bool, Any], None]) -> None:
    """Выполняет work() в потоке; on_done(ok, result_or_error) — в потоке GTK."""

    def worker() -> None:
        try:
            result: Any = work()
            ok = True
        except ApiError as err:
            result, ok = err, False
        except Exception as err:  # noqa: BLE001 - в UI не должно долетать ничего сырого
            result, ok = ApiError(str(err)), False

        # False из idle_add-колбэка = выполнить один раз и отцепиться.
        GLib.idle_add(lambda: (on_done(ok, result), False)[1])

    threading.Thread(target=worker, daemon=True).start()
