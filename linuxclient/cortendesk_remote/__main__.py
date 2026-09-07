"""Точка входа: python3 -m cortendesk_remote"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from .app import Application

    return Application().run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
