"""Настройки и сохранённый токен.

Пути — по XDG: настройки в ~/.config/cortendesk-remote, данные встроенного
браузера в ~/.local/share/cortendesk-remote.

Токен по возможности уходит в Secret Service (GNOME Keyring, KWallet и прочие
через python3-secretstorage) — это линуксовый аналог DPAPI из Windows-версии.
Там, где Secret Service не запущен (headless-машина, чистый оконный менеджер
без агента ключей), токен пишется в файл с правами 0600 в приватном каталоге.
Это слабее, и приложение об этом честно говорит; отключить файловый запасной
путь можно, сняв галку «Запомнить вход».
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

APP_ID = "cortendesk-remote"
SETTINGS_FILE = "settings.json"
TOKEN_FILE = "token"

#: Атрибуты записи в Secret Service — по ним она ищется при следующем запуске.
SECRET_ATTRS = {"application": APP_ID, "what": "client-api-token"}

DEFAULTS: dict = {
    "server_url": "https://rd.spritelutsk.duckdns.org",
    "username": "",
    "remember_me": True,
    "device_uuid": "",
    "auto_refresh_seconds": 15,
    "online_only": False,
}


def config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / APP_ID


def data_dir() -> Path:
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / APP_ID


def webview_dir() -> Path:
    """Профиль WebKit: тут копится cookie сессии консоли."""
    return data_dir() / "webkit"


# ---- настройки ---------------------------------------------------------


def load_settings() -> dict:
    settings = dict(DEFAULTS)

    try:
        stored = json.loads((config_dir() / SETTINGS_FILE).read_text("utf-8"))
        if isinstance(stored, dict):
            settings.update(stored)
    except (OSError, ValueError):
        pass  # файла нет или он битый — работаем на значениях по умолчанию

    if not settings.get("device_uuid"):
        settings["device_uuid"] = uuid.uuid4().hex

    if not isinstance(settings.get("auto_refresh_seconds"), int) or settings["auto_refresh_seconds"] < 5:
        settings["auto_refresh_seconds"] = 15

    return settings


def save_settings(settings: dict) -> None:
    try:
        config_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        (config_dir() / SETTINGS_FILE).write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), "utf-8"
        )
    except OSError:
        pass  # не сохранилось — приложение работает, просто забудет выбор


def device_id(settings: dict) -> str:
    return "linux-" + settings["device_uuid"]


# ---- токен -------------------------------------------------------------


def _collection():
    """Открытая коллекция Secret Service или None, если его нет."""
    import secretstorage

    bus = secretstorage.dbus_init()
    collection = secretstorage.get_default_collection(bus)

    if collection.is_locked():
        collection.unlock()

    return collection


def storage_kind() -> str:
    """'keyring' | 'file' — что реально будет использовано. Для текста в UI."""
    try:
        _collection()
        return "keyring"
    except Exception:  # noqa: BLE001 - причин отказа у D-Bus много, различать их незачем
        return "file"


def save_token(token: str) -> None:
    try:
        _collection().create_item(
            "CortenDesk Remote — токен клиентского API",
            SECRET_ATTRS,
            token.encode("utf-8"),
            replace=True,
        )
        return
    except Exception:  # noqa: BLE001
        pass

    # Запасной путь: приватный каталог и файл только для владельца.
    #
    # Через write_text + chmod так делать нельзя: файл создаётся по umask
    # (обычно 0644) и какое-то время лежит с токеном и правами на чтение всем,
    # а дескриптор, открытый в этом окне, переживёт последующий chmod. Пишем
    # во временный файл, созданный сразу с 0600, и подменяем через os.replace —
    # он атомарен в пределах файловой системы.
    #
    # mkdir(mode=...) не исправляет права УЖЕ существующего каталога и не
    # применяется к родителям, а каталог мог быть создан раньше из session.py
    # по umask, — поэтому режим выставляется отдельным chmod.
    try:
        directory = data_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)

        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".token-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
            os.replace(tmp, directory / TOKEN_FILE)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError:
        pass


def load_token() -> str | None:
    try:
        for item in _collection().search_items(SECRET_ATTRS):
            return item.get_secret().decode("utf-8")
    except Exception:  # noqa: BLE001
        pass

    try:
        return (data_dir() / TOKEN_FILE).read_text("utf-8").strip() or None
    except OSError:
        return None


def clear_token() -> None:
    try:
        for item in _collection().search_items(SECRET_ATTRS):
            item.delete()
    except Exception:  # noqa: BLE001
        pass

    try:
        (data_dir() / TOKEN_FILE).unlink(missing_ok=True)
    except OSError:
        pass
