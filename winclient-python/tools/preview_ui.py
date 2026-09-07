#!/usr/bin/env python3
"""Показать интерфейс без сервера и без учётной записи.

Список устройств набивается выдуманными данными, поэтому экран можно
посмотреть и поправить, не имея доступа к консоли CortenDesk.

    python tools/preview_ui.py           # список устройств
    python tools/preview_ui.py login     # окно входа
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cortendesk_remote.ui.app import App  # noqa: E402


class FakeApi:
    """Ровно те методы, которые дёргает DevicesFrame."""

    base = "https://rd.spritelutsk.duckdns.org"
    host = "rd.spritelutsk.duckdns.org"
    token = "fake"

    def peers(self) -> list[dict]:
        return [
            {"id": "123456789", "status": 1, "user_name": "anna",
             "device_group_name": "Бухгалтерия", "note": "кассовое место",
             "info": {"device_name": "BUH-PC", "os": "Windows 11", "username": "buh"}},
            {"id": "987654321", "status": 0, "user_name": "ivan",
             "device_group_name": "Склад", "note": "",
             "info": {"device_name": "SKLAD-01", "os": "Windows 10", "username": "sklad"}},
            {"id": "555000111", "status": 1, "user_name": "admin",
             "device_group_name": "Дирекция", "note": "не перезагружать",
             "info": {"device_name": "DIRECTOR", "os": "Windows 11", "username": "boss"}},
            {"id": "222333444", "status": 1, "user_name": "anna",
             "device_group_name": "Бухгалтерия", "note": "",
             "info": {"device_name": "KASSA-2", "os": "Windows 10", "username": "kassa"}},
        ]

    def device_groups(self) -> list[str]:
        return ["Бухгалтерия", "Дирекция", "Склад"]

    def web_client_url(self, peer_id: str) -> str:
        return f"{self.base}/webclient?id={peer_id}"

    def logout(self) -> None:
        pass


def main() -> int:
    app = App()

    if not (len(sys.argv) > 1 and sys.argv[1] == "login"):
        app.api = FakeApi()
        app.user = {"name": "admin", "display_name": "Администратор", "is_admin": True}
        app.show_devices()

    return app.run()


if __name__ == "__main__":
    sys.exit(main())
