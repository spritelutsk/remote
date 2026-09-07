"""Окно сеанса: нативный веб-клиент CortenDesk во встроенном WebKit2GTK.

В отличие от Windows-версий, здесь браузер живёт в том же процессе и в том же
цикле событий GTK — отдельный процесс не нужен, потому что WebKit2GTK и есть
часть GTK-стека. Заодно cookie консоли хранится в собственном профиле
WebsiteDataManager, так что вход спрашивается только в первый раз.

Разбор навигации повторяет логику остальных версий. Порядок ветвлений важен:
сначала успех, потом чужой хост (портал-провайдер SSO — туда лезть нельзя),
потом формы входа консоли, и только оставшееся считается «вошли, но не туда» —
это редирект на главную после логина, откуда мы возвращаемся сами.
"""

from __future__ import annotations

import os
import urllib.parse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")

from gi.repository import Gdk, Gtk, WebKit2  # noqa: E402

from . import store  # noqa: E402

#: Сколько раз возвращаться на /webclient, если сервер увёл на другую страницу.
MAX_RESUME_ATTEMPTS = 3

_AUTH_PREFIXES = ("/login", "/oidc", "/invite", "/forgot-password", "/reset-password")


def is_auth_page(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.startswith(prefix) for prefix in _AUTH_PREFIXES)


def _web_context() -> WebKit2.WebContext:
    """Контекст с постоянным профилем: cookie переживают перезапуск."""
    profile = store.webview_dir()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mode у mkdir не применяется к уже существующему каталогу и не касается
    # родителей — а именно здесь родитель обычно и создаётся впервые.
    os.chmod(store.data_dir(), 0o700)
    os.chmod(profile, 0o700)

    manager = WebKit2.WebsiteDataManager(
        base_data_directory=str(profile),
        base_cache_directory=str(profile / "cache"),
    )
    context = WebKit2.WebContext.new_with_website_data_manager(manager)

    context.get_cookie_manager().set_persistent_storage(
        str(profile / "cookies.sqlite"), WebKit2.CookiePersistentStorage.SQLITE
    )

    return context


def _origin_of(url: str) -> str | None:
    """Origin адреса; None для не-URL и для схем вроде file: и javascript:."""
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None

    return f"{parts.scheme}://{parts.netloc}"


def _harden(settings: WebKit2.Settings) -> None:
    """Выписать ограничения явно.

    По умолчанию WebKit и так их держит выключенными, но посмотреть на код и
    понять, что разрешено веб-клиенту, было нельзя, а молчаливая смена
    умолчаний при обновлении WebKit прошла бы незамеченной.
    """
    settings.set_enable_developer_extras(False)
    settings.set_allow_file_access_from_file_urls(False)
    settings.set_allow_universal_access_from_file_urls(False)
    settings.set_javascript_can_open_windows_automatically(False)
    settings.set_javascript_can_access_clipboard(True)   # нужно веб-клиенту
    settings.set_enable_write_console_messages_to_stdout(False)


class SessionWindow(Gtk.Window):
    def __init__(self, url: str, peer_name: str, peer_id: str) -> None:
        super().__init__(title=f"{peer_name} ({peer_id}) — CortenDesk Remote")

        self.target = url
        self.console_host = urllib.parse.urlsplit(url).netloc
        self.attempts = 0
        self.fullscreen_on = False

        self.set_default_size(1280, 820)

        header = Gtk.HeaderBar(show_close_button=True)
        header.set_title(peer_name)
        header.set_subtitle(peer_id)
        self.set_titlebar(header)

        reconnect = Gtk.Button(label="Переподключиться")
        reconnect.connect("clicked", lambda _b: self.reconnect())
        header.pack_start(reconnect)

        full = Gtk.Button(label="Во весь экран")
        full.set_tooltip_text("F11")
        full.connect("clicked", lambda _b: self.toggle_fullscreen())
        header.pack_end(full)

        self.view = WebKit2.WebView.new_with_context(_web_context())
        _harden(self.view.get_settings())
        self.view.connect("load-changed", self.on_load_changed)
        self.view.connect("decide-policy", self.on_decide_policy)

        # Origin консоли разрешён всегда; чужой добавляется только как цель
        # серверного редиректа с уже разрешённой страницы — это нога SSO.
        self.allowed_origins = {_origin_of(url)} - {None}

        self.add(self.view)

        self.connect("key-press-event", self.on_key_press)

        self.view.load_uri(self.target)

    # ---- поведение -----------------------------------------------------

    def on_load_changed(self, _view, event) -> None:
        if event != WebKit2.LoadEvent.FINISHED:
            return

        current = self.view.get_uri()
        if not current:
            return

        parts = urllib.parse.urlsplit(current)

        if parts.path.lower() == "/webclient":
            self.attempts = 0
            return

        if parts.netloc != self.console_host:
            return  # внешний провайдер SSO — не мешаем

        if is_auth_page(parts.path):
            return  # форма входа — ждём пользователя

        if self.attempts < MAX_RESUME_ATTEMPTS:
            self.attempts += 1
            self.view.load_uri(self.target)
            return

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text="Не удалось открыть веб-клиент",
        )
        dialog.format_secondary_text(
            "Сервер увёл нас со страницы сеанса. Проверьте, что у вашей учётной "
            f"записи есть доступ к устройству {self.get_title()}."
        )
        dialog.run()
        dialog.destroy()

    def on_decide_policy(self, _view, decision, decision_type) -> bool:
        """Куда окну позволено ходить.

        Одного origin консоли мало: вход идёт через SSO на портал, это другой
        хост. Поэтому цель разрешается, если она уже в списке; а список
        пополняется только адресами, на которые нас увёл сам сервер.
        """
        if decision_type not in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                                 WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False   # ответы на запросы ресурсов решает WebKit

        action = decision.get_navigation_action()
        target = _origin_of(action.get_request().get_uri())

        if target is None:
            decision.ignore()      # file:, javascript: и прочее — мимо
            return True

        if target in self.allowed_origins:
            decision.use()
            return True

        # Серверный редирект (не действие пользователя и не скрипт) с уже
        # разрешённой страницы — шаг SSO: запоминаем и пропускаем.
        current = _origin_of(self.view.get_uri() or "")
        if action.is_redirect() and current in self.allowed_origins:
            self.allowed_origins.add(target)
            decision.use()
            return True

        decision.ignore()
        return True

    def reconnect(self) -> None:
        self.attempts = 0
        self.view.load_uri(self.target)

    def toggle_fullscreen(self) -> None:
        self.fullscreen_on = not self.fullscreen_on
        if self.fullscreen_on:
            self.fullscreen()
        else:
            self.unfullscreen()

    def on_key_press(self, _widget, event) -> bool:
        # F11 — как в браузере; Esc выходит из полноэкранного режима, но не
        # закрывает окно: во время сеанса Esc нужен удалённой машине.
        if event.keyval == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True

        if event.keyval == Gdk.KEY_Escape and self.fullscreen_on:
            self.toggle_fullscreen()
            return True

        return False
