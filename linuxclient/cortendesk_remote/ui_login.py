"""Экран входа в клиентский API CortenDesk.

Если с прошлого запуска остался токен, экран сначала молча проверяет его через
/api/currentUser и, если он жив, сразу отдаёт управление списку устройств.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import Gtk  # noqa: E402

from . import store  # noqa: E402
from .api import ApiError, CortenDeskApi  # noqa: E402
from .tasks import run_async  # noqa: E402


class LoginPage(Gtk.Box):
    def __init__(self, app) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.app = app
        self.settings = app.settings

        # Без явных полей содержимое прилипает к краям окна: у Gtk.Box, в
        # отличие от контейнеров с рамкой, отступов по умолчанию нет.
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER,
                        valign=Gtk.Align.CENTER, margin=28)
        outer.set_size_request(360, -1)
        self.pack_start(outer, True, True, 0)

        title = Gtk.Label(xalign=0)
        title.set_markup("<span size='x-large' weight='bold'>CortenDesk Remote</span>")
        outer.pack_start(title, False, False, 0)

        subtitle = Gtk.Label(label="Удалённое управление рабочими столами", xalign=0)
        subtitle.get_style_context().add_class("dim-label")
        outer.pack_start(subtitle, False, False, 0)

        grid = Gtk.Grid(row_spacing=6, column_spacing=8, margin_top=18)
        outer.pack_start(grid, False, False, 0)

        self.server = self._row(grid, 0, "Сервер", self.settings["server_url"])
        self.username = self._row(grid, 2, "Пользователь", self.settings["username"])
        self.password = self._row(grid, 4, "Пароль", "", visibility=False)

        self.remember = Gtk.CheckButton(label="Запомнить вход на этом компьютере")
        self.remember.set_active(bool(self.settings["remember_me"]))
        self.remember.set_margin_top(8)
        outer.pack_start(self.remember, False, False, 0)

        # Куда именно ляжет токен, зависит от того, есть ли Secret Service.
        # Пользователь имеет право знать: файловый путь заметно слабее.
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.get_style_context().add_class("dim-label")
        hint.set_markup(
            "<small>Токен будет сохранён в связке ключей.</small>"
            if store.storage_kind() == "keyring"
            else "<small>Связка ключей недоступна — токен ляжет в файл "
                 "с правами 0600 в ~/.local/share/cortendesk-remote.</small>"
        )
        outer.pack_start(hint, False, False, 0)

        self.error = Gtk.Label(xalign=0, wrap=True, margin_top=8)
        self.error.get_style_context().add_class("error-text")
        outer.pack_start(self.error, False, False, 0)

        self.submit = Gtk.Button(label="Войти", margin_top=12)
        self.submit.get_style_context().add_class("suggested-action")
        self.submit.connect("clicked", lambda _b: self.on_submit())
        outer.pack_start(self.submit, False, False, 0)

        self.status = Gtk.Label(xalign=0, margin_top=10)
        self.status.get_style_context().add_class("dim-label")
        outer.pack_start(self.status, False, False, 0)

        for entry in (self.server, self.username, self.password):
            entry.connect("activate", lambda _e: self.on_submit())

        self.show_error("")

    def _row(self, grid: Gtk.Grid, row: int, label: str, value: str,
             visibility: bool = True) -> Gtk.Entry:
        caption = Gtk.Label(label=label, xalign=0)
        grid.attach(caption, 0, row, 1, 1)

        entry = Gtk.Entry(hexpand=True)
        entry.set_text(value)
        entry.set_visibility(visibility)
        grid.attach(entry, 0, row + 1, 1, 1)

        return entry

    # ---- поведение -----------------------------------------------------

    def start(self) -> None:
        """Вызывается после показа окна: пробуем сохранённый токен."""
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
            self.set_busy(False, "")
            self.focus_first_empty()

        run_async(api.current_user, done)

    def focus_first_empty(self) -> None:
        (self.username if not self.username.get_text() else self.password).grab_focus()

    def set_busy(self, busy: bool, status: str = "") -> None:
        for widget in (self.submit, self.server, self.username, self.password):
            widget.set_sensitive(not busy)

        self.status.set_text(status)

    def show_error(self, message: str) -> None:
        self.error.set_text(message)
        self.error.set_visible(bool(message))
        self.error.set_no_show_all(not message)

    def on_submit(self) -> None:
        self.show_error("")

        username = self.username.get_text().strip()
        password = self.password.get_text()

        if not username or not password:
            self.show_error("Введите логин и пароль.")
            return

        self.set_busy(True, "Подключаемся к серверу…")

        try:
            api = CortenDeskApi(self.server.get_text())
        except ApiError as err:
            self.set_busy(False, "")
            self.show_error(err.message)
            return

        def work():
            return api.login(username, password,
                             store.device_id(self.settings), self.settings["device_uuid"])

        def done(ok: bool, result) -> None:
            if not ok:
                self.set_busy(False, "")
                self.show_error(result.message)
                return

            self.settings["server_url"] = api.base
            self.settings["username"] = username
            self.settings["remember_me"] = self.remember.get_active()
            store.save_settings(self.settings)

            if self.settings["remember_me"]:
                store.save_token(api.token)
            else:
                store.clear_token()

            self.app.on_authenticated(api, result)

        run_async(work, done)
