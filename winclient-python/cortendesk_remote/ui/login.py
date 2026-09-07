"""Окно входа в клиентский API CortenDesk.

Если с прошлого запуска остался токен, форма сначала молча проверяет его через
/api/currentUser и, если он жив, сразу отдаёт управление списку устройств —
пользователь формы в этом случае даже не увидит.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import store
from ..api import CortenDeskApi
from . import theme
from .tasks import run_async


class LoginFrame(ttk.Frame):
    def __init__(self, master, app) -> None:
        super().__init__(master, style="TFrame", padding=18)

        self.app = app
        self.settings = app.settings

        card = ttk.Frame(self, style="Card.TFrame", padding=24)
        card.pack(fill="both", expand=True)

        ttk.Label(card, text="CortenDesk Remote", style="H1.TLabel").pack(anchor="w")
        ttk.Label(
            card,
            text="Удалённое управление рабочими столами",
            style="CardMuted.TLabel",
        ).pack(anchor="w", pady=(4, 16))

        self.server = self._field(card, "Сервер", self.settings["server_url"])
        self.username = self._field(card, "Пользователь", self.settings["username"])
        self.password = self._field(card, "Пароль", "", show="•")

        self.remember = tk.BooleanVar(value=bool(self.settings["remember_me"]))
        ttk.Checkbutton(
            card,
            text="Запомнить вход на этом компьютере",
            variable=self.remember,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(4, 12))

        self.error = ttk.Label(card, text="", style="Error.TLabel", wraplength=320)

        self.submit = ttk.Button(card, text="Войти", style="Accent.TButton", command=self.on_submit)
        self.submit.pack(fill="x")

        self.status = ttk.Label(card, text="", style="CardMuted.TLabel", wraplength=320)
        self.status.pack(anchor="w", pady=(12, 0))

        # Enter в любом поле — как нажатие «Войти».
        for widget in (self.server, self.username, self.password):
            widget.bind("<Return>", lambda _event: self.on_submit())

        self.after(50, self.try_stored_login)

    def _field(self, parent, label: str, value: str, show: str | None = None) -> ttk.Entry:
        ttk.Label(parent, text=label, style="Card.TLabel").pack(anchor="w")

        entry = ttk.Entry(parent, show=show) if show else ttk.Entry(parent)
        entry.insert(0, value)
        entry.pack(fill="x", pady=(3, 10))
        return entry

    # ---- поведение -----------------------------------------------------

    def set_busy(self, busy: bool, status: str = "") -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.submit, self.server, self.username, self.password):
            widget.configure(state=state)

        self.status.configure(text=status)

    def show_error(self, message: str) -> None:
        self.error.configure(text=message)
        self.error.pack(anchor="w", pady=(0, 10), before=self.submit)

    def hide_error(self) -> None:
        self.error.pack_forget()

    def focus_first_empty(self) -> None:
        (self.username if not self.username.get() else self.password).focus_set()

    def try_stored_login(self) -> None:
        token = store.load_token()
        if not token:
            self.focus_first_empty()
            return

        self.set_busy(True, "Проверяем сохранённый вход…")
        api = CortenDeskApi(self.settings["server_url"], token)

        def done(ok: bool, result) -> None:
            if ok:
                self.app.on_authenticated(api, result)
                return

            # Любая осечка на сохранённом токене — просто показываем форму.
            store.clear_token()
            self.set_busy(False)
            self.focus_first_empty()

        run_async(self, api.current_user, done)

    def on_submit(self) -> None:
        self.hide_error()

        username = self.username.get().strip()
        password = self.password.get()

        if not username or not password:
            self.show_error("Введите логин и пароль.")
            return

        self.set_busy(True, "Подключаемся к серверу…")

        try:
            api = CortenDeskApi(self.server.get())
        except Exception as err:  # noqa: BLE001 - сюда долетает только ApiError адреса
            self.set_busy(False)
            self.show_error(str(err))
            return

        def work():
            return api.login(
                username,
                password,
                store.device_id(self.settings),
                self.settings["device_uuid"],
            )

        def done(ok: bool, result) -> None:
            if not ok:
                self.set_busy(False)
                self.show_error(result.message)
                return

            self.settings["server_url"] = api.base
            self.settings["username"] = username
            self.settings["remember_me"] = bool(self.remember.get())
            store.save_settings(self.settings)

            if self.settings["remember_me"]:
                store.save_token(api.token)
            else:
                store.clear_token()

            self.app.on_authenticated(api, result)

        run_async(self, work, done)
