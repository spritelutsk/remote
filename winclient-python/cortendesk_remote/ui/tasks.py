"""Сетевые вызовы в фоновом потоке.

Tk не потокобезопасен: трогать виджеты можно только из главного потока.
Поэтому работа уходит в поток, а результат возвращается через widget.after(0,…),
то есть уже в цикле событий Tk.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from ..api import ApiError


def run_async(
    widget,
    work: Callable[[], Any],
    on_done: Callable[[bool, Any], None],
) -> None:
    """Выполняет work() в потоке; on_done(ok, result_or_error) — в потоке Tk."""

    def worker() -> None:
        try:
            result: Any = work()
            ok = True
        except ApiError as err:
            result, ok = err, False
        except Exception as err:  # noqa: BLE001 - в UI не должно долетать ничего сырого
            result, ok = ApiError(str(err)), False

        try:
            widget.after(0, lambda: on_done(ok, result))
        except RuntimeError:
            # Окно уже закрыто, цикла событий нет — результат никому не нужен.
            pass

    threading.Thread(target=worker, daemon=True).start()
