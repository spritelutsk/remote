"""Приложение: одно окно, две страницы в Gtk.Stack.

Окна сеансов — отдельные top-level окна в том же процессе: WebKit2GTK живёт
в общем цикле событий GTK, отдельный процесс (как на Windows) здесь не нужен.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, Gio, Gtk  # noqa: E402

from . import store  # noqa: E402
from .ui_devices import DevicesPage  # noqa: E402
from .ui_login import LoginPage  # noqa: E402

APP_ID = "org.spritelutsk.CortenDeskRemote"

CSS = b"""
.error-text { color: #d63a4e; }
"""


class Application(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

        self.settings = store.load_settings()
        store.save_settings(self.settings)   # фиксируем сгенерированный device_uuid

        self.api = None
        self.user: dict | None = None
        self.window: Gtk.ApplicationWindow | None = None
        self.page: Gtk.Widget | None = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def do_activate(self) -> None:
        if self.window is not None:
            self.window.present()
            return

        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_default_size(420, 480)

        self.header = Gtk.HeaderBar(show_close_button=True, title="CortenDesk Remote")
        self.window.set_titlebar(self.header)

        self.logout_button = Gtk.Button(label="Выйти")
        self.logout_button.connect("clicked", lambda _b: self.logout())

        self.container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.window.add(self.container)

        self.show_login()
        self.window.show_all()

        # Токен проверяем после показа окна, иначе на медленной сети
        # приложение стартует «в пустоту».
        self.page.start()

    # ---- страницы ------------------------------------------------------

    def _swap(self, page: Gtk.Widget, width: int, height: int) -> None:
        if self.page is not None:
            stop = getattr(self.page, "stop", None)
            if stop:
                stop()
            self.container.remove(self.page)
            self.page.destroy()

        self.page = page
        self.container.pack_start(page, True, True, 0)
        self.window.resize(width, height)
        page.show_all()

    def show_login(self) -> None:
        self.header.set_subtitle(None)

        if self.logout_button.get_parent() is not None:
            self.header.remove(self.logout_button)

        self._swap(LoginPage(self), 420, 480)

    def show_devices(self) -> None:
        user = self.user or {}
        title = user.get("display_name") or user.get("name") or ""
        if user.get("is_admin"):
            title = f"{title} · администратор"

        self.header.set_subtitle(f"{self.api.host} · {title}")

        if self.logout_button.get_parent() is None:
            self.header.pack_end(self.logout_button)
            self.logout_button.show()

        self._swap(DevicesPage(self), 1100, 660)

    # ---- события -------------------------------------------------------

    def on_authenticated(self, api, user: dict) -> None:
        self.api = api
        self.user = user
        self.show_devices()

    def on_auth_expired(self) -> None:
        store.clear_token()

        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text="Сессия на сервере истекла",
        )
        dialog.format_secondary_text("Войдите заново.")
        dialog.run()
        dialog.destroy()

        self.api = None
        self.user = None
        self.show_login()
        self.page.start()

    def logout(self) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Выйти из учётной записи?",
        )
        dialog.format_secondary_text("Сохранённый вход будет удалён.")
        answer = dialog.run()
        dialog.destroy()

        if answer != Gtk.ResponseType.YES:
            return

        store.clear_token()
        if self.api is not None:
            self.api.logout()

        self.api = None
        self.user = None
        self.show_login()
