"""Оркестратор окна: вход -> список устройств.

Одно окно Tk на всё приложение, содержимое подменяется. Два независимых Tk()
в одном процессе — источник трудноуловимых проблем, а окно сеанса всё равно
живёт отдельным процессом (см. launcher).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .. import store
from . import theme
from .devices import DevicesFrame
from .login import LoginFrame


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("CortenDesk Remote")
        self.root.configure(bg=theme.BG)

        theme.apply(self.root)

        self.settings = store.load_settings()
        store.save_settings(self.settings)   # фиксируем сгенерированный device_uuid

        self.api = None
        self.user: dict | None = None
        self.frame: ttk.Frame | None = None

        self.show_login()

    # ---- переключение экранов ------------------------------------------

    def _swap(self, frame: ttk.Frame, width: int, height: int, resizable: bool) -> None:
        if self.frame is not None:
            stop = getattr(self.frame, "stop", None)
            if stop:
                stop()
            self.frame.destroy()

        self.frame = frame
        frame.pack(fill="both", expand=True)

        self.root.geometry(f"{width}x{height}")
        self.root.minsize(width if not resizable else 820, height if not resizable else 480)
        self.root.resizable(resizable, resizable)

    def show_login(self) -> None:
        self.root.title("Вход — CortenDesk Remote")
        self._swap(LoginFrame(self.root, self), 420, 450, resizable=False)

    def show_devices(self) -> None:
        self.root.title("CortenDesk Remote")
        self._swap(DevicesFrame(self.root, self), 1140, 680, resizable=True)

    # ---- события -------------------------------------------------------

    def on_authenticated(self, api, user: dict) -> None:
        self.api = api
        self.user = user
        self.show_devices()

    def on_auth_expired(self) -> None:
        store.clear_token()
        messagebox.showwarning(
            "CortenDesk Remote",
            "Сессия на сервере истекла. Войдите заново.",
            parent=self.root,
        )

        self.api = None
        self.user = None
        self.show_login()

    def logout(self) -> None:
        confirmed = messagebox.askyesno(
            "CortenDesk Remote",
            "Выйти из учётной записи? Сохранённый вход будет удалён.",
            parent=self.root,
        )
        if not confirmed:
            return

        store.clear_token()
        if self.api is not None:
            self.api.logout()

        self.api = None
        self.user = None
        self.show_login()

    def run(self) -> int:
        self.root.mainloop()
        return 0
