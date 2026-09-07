"""Клиент REST API CortenDesk.

Только стандартная библиотека: urllib вместо requests — приложению и так
нужна лишь одна внешняя зависимость (pywebview), плодить вторую незачем.

Две особенности контракта, из-за которых мало проверить HTTP-статус:
  * ошибки приходят как {"error": "..."} даже с кодом 200 — так устроен
    клиентский протокол RustDesk, стоковый клиент статус не смотрит;
  * списочные ответы страничные: {"total": N, "data": [...]}, pageSize <= 500.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

PAGE_SIZE = 200
MAX_PAGES = 200          # предохранитель от бесконечного цикла
TIMEOUT = 30             # секунд на запрос
USER_AGENT = "CortenDeskRemote/1.0 (Windows; Python)"

#: Хосты, которым http разрешён: локальная отладка.
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ApiError(Exception):
    """Ошибка, которую можно показать пользователю как есть."""

    def __init__(self, message: str, expired: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.expired = expired


def _auth_expired() -> ApiError:
    return ApiError("Сессия на сервере истекла, войдите заново.", expired=True)


def normalize_base(raw: str) -> str:
    """Приводит введённый адрес к виду https://host[:port] без хвостового слэша."""
    trimmed = (raw or "").strip()
    if not trimmed:
        raise ApiError("Укажите адрес сервера.")

    if "://" not in trimmed:
        trimmed = "https://" + trimmed

    parts = urllib.parse.urlsplit(trimmed)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ApiError(f"Некорректный адрес сервера: {raw}")

    # По http логин и пароль ушли бы открытым текстом. Схему по умолчанию мы и
    # так подставляем https, но явный http надо отклонить — кроме петли, где он
    # нужен для отладки против локального сервера.
    if parts.scheme == "http" and (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
        raise ApiError(
            "Подключение по http небезопасно: логин и пароль уйдут открытым текстом. "
            "Укажите адрес с https://"
        )

    return f"{parts.scheme}://{parts.netloc}"


class CortenDeskApi:
    """Клиентский API консоли. Потокобезопасен настолько, насколько потокобезопасен
    urllib: каждый вызов открывает своё соединение и общего состояния не трогает,
    кроме `token`, который меняется только при входе и выходе."""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base = normalize_base(base_url)
        self.token = token

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.base).netloc

    def web_client_url(self, peer_id: str) -> str:
        return f"{self.base}/webclient?id={urllib.parse.quote(str(peer_id), safe='')}"

    # ---- эндпоинты -----------------------------------------------------

    def login(self, username: str, password: str, device_id: str, device_uuid: str) -> dict:
        """POST /api/login — выдаёт Bearer клиентского API."""
        payload = self._request(
            "POST",
            "/api/login",
            auth=False,
            body={
                "username": username,
                "password": password,
                "id": device_id,
                "uuid": device_uuid,
                "autoLogin": False,
                "type": "account",
                "verificationCode": "",
                "deviceInfo": {"os": "Windows", "type": "client", "name": "CortenDesk Remote"},
            },
        )

        if payload.get("error"):
            raise ApiError(_translate(payload["error"]))

        if payload.get("tfa_type"):
            raise ApiError(
                "Для этой учётной записи включена двухфакторная аутентификация, "
                "клиентский API её не поддерживает. Войдите через веб-консоль."
            )

        token = payload.get("access_token")
        if not token:
            raise ApiError("Сервер не вернул токен доступа.")

        self.token = token
        return payload.get("user") or {"name": username}

    def current_user(self) -> dict:
        """POST /api/currentUser — тихая проверка живости сохранённого токена."""
        return self._request("POST", "/api/currentUser")

    def peers(self) -> list[dict]:
        """GET /api/peers — устройства, видимые текущему пользователю."""
        return self._all_pages("/api/peers")

    def device_groups(self) -> list[str]:
        """GET /api/device-group/accessible — доступные папки устройств."""
        names = {row.get("name") for row in self._all_pages("/api/device-group/accessible")}
        return sorted(name for name in names if name)

    def logout(self) -> None:
        """POST /api/logout — отзывает токен, ошибки намеренно проглатываются."""
        if not self.token:
            return
        try:
            self._request("POST", "/api/logout")
        except ApiError:
            pass  # выход не должен падать из-за недоступного сервера
        finally:
            self.token = None

    # ---- транспорт -----------------------------------------------------

    def _all_pages(self, path: str) -> list[dict]:
        collected: list[dict] = []

        for page in range(1, MAX_PAGES + 1):
            payload = self._request("GET", f"{path}?current={page}&pageSize={PAGE_SIZE}")

            if payload.get("error"):
                raise ApiError(_translate(payload["error"]))

            data = payload.get("data") or []
            collected.extend(data)

            # Страница неполная или общее число уже набрано — дальше пусто.
            if len(data) < PAGE_SIZE or len(collected) >= int(payload.get("total") or 0):
                break

        return collected

    def _request(self, method: str, path: str, auth: bool = True, body: dict | None = None) -> dict:
        if auth and not self.token:
            raise _auth_expired()

        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as err:
            # Тело ошибки нужно прочитать здесь: HTTPError — это и есть ответ,
            # и у 401 в нём лежит осмысленный JSON.
            status, raw = err.code, err.read()
        except socket.timeout:
            raise ApiError("Сервер не ответил вовремя.") from None
        except urllib.error.URLError as err:
            raise ApiError(f"Не удалось связаться с сервером: {err.reason}") from None

        if status in (401, 403):
            raise _auth_expired()

        if status == 429:
            raise ApiError("Слишком много запросов к серверу, попробуйте через минуту.")

        text = raw.decode("utf-8", "replace").strip()
        if not text:
            if status >= 400:
                raise ApiError(f"Сервер ответил {status}.")
            raise ApiError("Сервер вернул пустой ответ.")

        try:
            return json.loads(text)
        except ValueError:
            # Типичный случай: вместо JSON прилетела HTML-страница логина или
            # ошибка прокси — показывать её сырой бессмысленно.
            if status < 400:
                raise ApiError(
                    "Сервер вернул не JSON. Проверьте адрес: "
                    "он должен указывать на консоль CortenDesk."
                ) from None
            raise ApiError(f"Сервер ответил {status} и вернул не JSON.") from None


def _translate(error: str) -> str:
    """Сообщения клиентского API приходят по-английски и коротко."""
    return "Неверный логин или пароль." if error == "Wrong credentials" else error
