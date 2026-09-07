"""Настройки и сохранённый токен в %APPDATA%\\CortenDeskRemote.

Токен лежит отдельным файлом и шифруется DPAPI — тем же механизмом, что и в
WPF-версии, только вызванным через ctypes, чтобы не тянуть pywin32. В
settings.json его класть нельзя: это обычный читаемый JSON, а токен клиентского
API открывает весь список устройств.

На не-Windows DPAPI нет, и токен просто не сохраняется: писать его открытым
текстом хуже, чем спросить пароль ещё раз. Приложение при этом работает —
модуль импортируется и на Linux, что нужно для прогонов и тестов.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

APP_DIR_NAME = "CortenDeskRemote"
SETTINGS_FILE = "settings.json"
TOKEN_FILE = "token.dat"

#: Энтропия DPAPI — привязывает шифртекст к этому приложению.
ENTROPY = b"CortenDeskRemote.v1"

DEFAULTS: dict = {
    "server_url": "https://rd.spritelutsk.duckdns.org",
    "username": "",
    "remember_me": True,
    "device_uuid": "",
    "auto_refresh_seconds": 15,
    "online_only": False,
}


def app_dir() -> Path:
    """%APPDATA%\\CortenDeskRemote на Windows, ~/.config/... в остальных случаях."""
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or str(Path.home())
    else:
        root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")

    return Path(root) / APP_DIR_NAME


def load_settings() -> dict:
    settings = dict(DEFAULTS)

    try:
        stored = json.loads((app_dir() / SETTINGS_FILE).read_text("utf-8"))
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
        app_dir().mkdir(parents=True, exist_ok=True)
        (app_dir() / SETTINGS_FILE).write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), "utf-8"
        )
    except OSError:
        pass  # не сохранилось — приложение работает, просто забудет выбор


def device_id(settings: dict) -> str:
    return "win-" + settings["device_uuid"]


# ---- токен под DPAPI ---------------------------------------------------


def encryption_available() -> bool:
    return sys.platform == "win32"


def save_token(token: str) -> None:
    if not encryption_available():
        return

    try:
        app_dir().mkdir(parents=True, exist_ok=True)
        (app_dir() / TOKEN_FILE).write_bytes(_dpapi_protect(token.encode("utf-8")))
    except (OSError, RuntimeError):
        pass  # следующий запуск просто спросит логин


def load_token() -> str | None:
    if not encryption_available():
        return None

    try:
        return _dpapi_unprotect((app_dir() / TOKEN_FILE).read_bytes()).decode("utf-8")
    except (OSError, RuntimeError):
        # Файл перенесли с другой машины или профиль сменился — расшифровать
        # нельзя, и это нормально: просто попросим войти заново.
        return None


def clear_token() -> None:
    try:
        (app_dir() / TOKEN_FILE).unlink(missing_ok=True)
    except OSError:
        pass


# ---- обвязка над CryptProtectData / CryptUnprotectData -----------------
#
# Оба вызова принимают и возвращают DATA_BLOB, а выделенный ими буфер надо
# освобождать через LocalFree — иначе утечка на каждый вызов.


def _blobs():
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32.dll")
    kernel32 = ctypes.WinDLL("kernel32.dll")

    # Прототипы объявляем явно. На x64 соглашение по умолчанию совпадает, и без
    # этого работает, но неявная зависимость от него — ровно тот случай, когда
    # ошибка вылезает при смене разрядности и выглядит необъяснимо.
    for fn in (crypt32.CryptProtectData, crypt32.CryptUnprotectData):
        fn.argtypes = [
            ctypes.POINTER(DataBlob),        # pDataIn
            wintypes.LPCWSTR,                # szDataDescr
            ctypes.POINTER(DataBlob),        # pOptionalEntropy
            ctypes.c_void_p,                 # pvReserved
            ctypes.c_void_p,                 # pPromptStruct
            wintypes.DWORD,                  # dwFlags
            ctypes.POINTER(DataBlob),        # pDataOut
        ]
        fn.restype = wintypes.BOOL

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    return ctypes, wintypes, DataBlob, crypt32, kernel32


def _to_blob(ctypes_mod, blob_type, data: bytes):
    buffer = ctypes_mod.create_string_buffer(data, len(data))
    return blob_type(len(data), ctypes_mod.cast(buffer, ctypes_mod.POINTER(ctypes_mod.c_char)))


def _from_blob(ctypes_mod, blob) -> bytes:
    return ctypes_mod.string_at(blob.pbData, blob.cbData)


def _dpapi_protect(data: bytes) -> bytes:
    ctypes_mod, wintypes, DataBlob, crypt32, kernel32 = _blobs()

    out = DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes_mod.byref(_to_blob(ctypes_mod, DataBlob, data)),
        None,
        ctypes_mod.byref(_to_blob(ctypes_mod, DataBlob, ENTROPY)),
        None,
        None,
        0,
        ctypes_mod.byref(out),
    )

    if not ok:
        raise RuntimeError("CryptProtectData failed")

    try:
        return _from_blob(ctypes_mod, out)
    finally:
        kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    ctypes_mod, wintypes, DataBlob, crypt32, kernel32 = _blobs()

    out = DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes_mod.byref(_to_blob(ctypes_mod, DataBlob, data)),
        None,
        ctypes_mod.byref(_to_blob(ctypes_mod, DataBlob, ENTROPY)),
        None,
        None,
        0,
        ctypes_mod.byref(out),
    )

    if not ok:
        raise RuntimeError("CryptUnprotectData failed")

    try:
        return _from_blob(ctypes_mod, out)
    finally:
        # Расшифрованный токен затираем до освобождения: LocalFree память не
        # чистит, и она вернётся в кучу процесса как есть.
        ctypes_mod.memset(out.pbData, 0, out.cbData)
        kernel32.LocalFree(out.pbData)
