"""Точка входа.

`--session <url> <title>` запускает окно сеанса: этот же модуль вызывается
дочерним процессом из launcher, чтобы pywebview получил свой главный поток.
Без аргументов открывается обычное приложение.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if args and args[0] == "--session":
        if len(args) < 2:
            print("Использование: --session <url> [заголовок]", file=sys.stderr)
            return 2

        from .session import run

        return run(args[1], args[2] if len(args) > 2 else "CortenDesk Remote")

    from .ui.app import App

    return App().run()


if __name__ == "__main__":
    sys.exit(main())
