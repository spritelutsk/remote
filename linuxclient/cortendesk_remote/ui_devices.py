"""Экран со списком устройств: таблица с присутствием, поиск, фильтры.

Модель собрана по канону GTK: ListStore с данными -> TreeModelFilter (поиск и
фильтры) -> TreeModelSort (сортировка по клику на заголовок) -> TreeView.
Это позволяет не перестраивать таблицу на каждое нажатие клавиши.

Присутствие на сервере обновляется примерно раз в 15 секунд (heartbeat
клиентов), поэтому и опрос идёт с тем же шагом — чаще смысла нет.
"""

from __future__ import annotations

from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import store  # noqa: E402
from .session import SessionWindow  # noqa: E402
from .tasks import run_async  # noqa: E402

# Колонки ListStore.
(COL_DOT, COL_DOT_COLOR, COL_NAME, COL_ID, COL_OS,
 COL_USER, COL_OWNER, COL_GROUP, COL_NOTE, COL_ONLINE) = range(10)

ONLINE_COLOR = "#159a63"
OFFLINE_COLOR = "#a6b0be"

#: (заголовок, колонка модели, минимальная ширина, растягивать ли)
#
# Минимум обязателен: с ellipsize и set_expand только на двух колонках
# остальные ужимаются до многоточия, и ID с названием ОС становятся
# нечитаемыми — «123…», «Win…».
VISIBLE_COLUMNS = (
    ("Устройство", COL_NAME, 180, True),
    ("ID", COL_ID, 110, False),
    ("ОС", COL_OS, 110, False),
    ("Пользователь", COL_USER, 120, False),
    ("Владелец", COL_OWNER, 110, False),
    ("Папка", COL_GROUP, 130, False),
    ("Заметка", COL_NOTE, 160, True),
)

SEARCH_COLUMNS = (COL_NAME, COL_ID, COL_OS, COL_USER, COL_OWNER, COL_GROUP, COL_NOTE)


class DevicesPage(Gtk.Box):
    def __init__(self, app) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.app = app
        self.api = app.api
        self.settings = app.settings

        self.refreshing = False
        self.timer_id: int | None = None
        self.sessions: list[SessionWindow] = []

        self.store = Gtk.ListStore(str, str, str, str, str, str, str, str, str, bool)

        self.filtered = self.store.filter_new()
        self.filtered.set_visible_func(self.is_visible)

        self.sorted = Gtk.TreeModelSort(model=self.filtered)
        self.sorted.set_sort_column_id(COL_NAME, Gtk.SortType.ASCENDING)

        self._build_filters()
        self._build_table()
        self._build_status()

        self.load_groups()
        self.refresh(silent=False)
        self.start_timer()

    # ---- разметка ------------------------------------------------------

    def _build_filters(self) -> None:
        bar = Gtk.Box(spacing=8, margin=10)
        self.pack_start(bar, False, False, 0)

        self.search = Gtk.SearchEntry(placeholder_text="Поиск по всем полям")
        self.search.set_width_chars(28)
        self.search.connect("search-changed", lambda _e: self.filtered.refilter())
        bar.pack_start(self.search, False, False, 0)

        self.group = Gtk.ComboBoxText()
        self.group.append_text("Все папки")
        self.group.set_active(0)
        self.group.connect("changed", lambda _c: self.filtered.refilter())
        bar.pack_start(self.group, False, False, 0)

        self.online_only = Gtk.CheckButton(label="Только в сети")
        self.online_only.set_active(bool(self.settings["online_only"]))
        self.online_only.connect("toggled", self.on_online_only)
        bar.pack_start(self.online_only, False, False, 0)

        refresh = Gtk.Button(label="Обновить")
        refresh.connect("clicked", lambda _b: self.refresh(silent=False))
        bar.pack_start(refresh, False, False, 0)

        self.connect_button = Gtk.Button(label="Подключиться")
        self.connect_button.get_style_context().add_class("suggested-action")
        self.connect_button.set_sensitive(False)
        self.connect_button.connect("clicked", lambda _b: self.connect_selected())
        bar.pack_end(self.connect_button, False, False, 0)

    def _build_table(self) -> None:
        scroller = Gtk.ScrolledWindow(margin_start=10, margin_end=10, margin_bottom=10)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        self.pack_start(scroller, True, True, 0)

        self.tree = Gtk.TreeView(model=self.sorted, headers_visible=True)
        self.tree.set_search_column(COL_NAME)
        scroller.add(self.tree)

        # Точка присутствия: цвет берётся из отдельной колонки модели, поэтому
        # красится ровно она, а не вся строка.
        dot = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("", dot, text=COL_DOT, foreground=COL_DOT_COLOR)
        column.set_fixed_width(28)
        column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.tree.append_column(column)

        for title, index, min_width, expand in VISIBLE_COLUMNS:
            renderer = Gtk.CellRendererText(ellipsize=3)  # PANGO_ELLIPSIZE_END
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_sort_column_id(index)
            column.set_resizable(True)
            column.set_min_width(min_width)
            column.set_expand(expand)
            self.tree.append_column(column)

        self.tree.get_selection().connect("changed", self.on_selection_changed)
        self.tree.connect("row-activated", lambda *_a: self.connect_selected())
        self.tree.connect("button-press-event", self.on_button_press)

        self.menu = Gtk.Menu()
        for label, handler in (
            ("Подключиться", self.connect_selected),
            ("Копировать ID", self.copy_id),
            (None, None),
            ("Открыть в браузере", self.open_in_browser),
        ):
            if label is None:
                item = Gtk.SeparatorMenuItem()
            else:
                item = Gtk.MenuItem(label=label)
                item.connect("activate", lambda _i, h=handler: h())
            self.menu.append(item)
        self.menu.show_all()

    def _build_status(self) -> None:
        self.status = Gtk.Label(xalign=0, margin=8)
        self.status.get_style_context().add_class("dim-label")
        self.pack_start(self.status, False, False, 0)

    # ---- фильтрация ----------------------------------------------------

    def is_visible(self, model, row_iter, _data=None) -> bool:
        if self.online_only.get_active() and not model[row_iter][COL_ONLINE]:
            return False

        chosen = self.group.get_active_text()
        if chosen and chosen != "Все папки" and model[row_iter][COL_GROUP] != chosen:
            return False

        needle = self.search.get_text().strip().lower()
        if not needle:
            return True

        return any(needle in (model[row_iter][index] or "").lower() for index in SEARCH_COLUMNS)

    def on_online_only(self, _button) -> None:
        self.settings["online_only"] = self.online_only.get_active()
        store.save_settings(self.settings)
        self.filtered.refilter()

    # ---- данные --------------------------------------------------------

    def load_groups(self) -> None:
        def done(ok: bool, result) -> None:
            # Прав на список папок может не быть — это не повод ломать экран.
            if not ok:
                return

            for name in result:
                self.group.append_text(name)

        run_async(self.api.device_groups, done)

    def refresh(self, silent: bool) -> None:
        if self.refreshing:
            return

        self.refreshing = True
        if not silent:
            self.status.set_text("Загружаем список устройств…")

        def done(ok: bool, result) -> None:
            self.refreshing = False

            if not ok:
                if getattr(result, "expired", False):
                    self.app.on_auth_expired()
                    return

                # Обрыв связи не должен глушить автообновление: следующая
                # попытка придёт по таймеру.
                self.status.set_text(f"Не удалось обновить список: {result.message}")
                return

            selected = self.selected_id()

            self.store.clear()
            online = 0

            for peer in result:
                info = peer.get("info") or {}
                is_online = peer.get("status") == 1
                online += is_online

                self.store.append([
                    "●" if is_online else "○",
                    ONLINE_COLOR if is_online else OFFLINE_COLOR,
                    info.get("device_name") or peer.get("id") or "",
                    peer.get("id") or "",
                    info.get("os") or "",
                    info.get("username") or "",
                    peer.get("user_name") or peer.get("user") or "",
                    peer.get("device_group_name") or "",
                    peer.get("note") or "",
                    is_online,
                ])

            if selected:
                self.select_by_id(selected)

            now = datetime.now().strftime("%H:%M:%S")
            self.status.set_text(
                f"Устройств: {len(result)} · в сети: {online} · обновлено в {now}"
            )

        run_async(self.api.peers, done)

    def start_timer(self) -> None:
        seconds = int(self.settings["auto_refresh_seconds"])
        self.timer_id = GLib.timeout_add_seconds(seconds, self._tick)

    def _tick(self) -> bool:
        self.refresh(silent=True)
        return True  # True = повторять

    def stop(self) -> None:
        if self.timer_id is not None:
            GLib.source_remove(self.timer_id)
            self.timer_id = None

    # ---- выделение и действия ------------------------------------------

    def selected_row(self):
        model, row_iter = self.tree.get_selection().get_selected()
        return (model, row_iter) if row_iter is not None else (None, None)

    def selected_id(self) -> str | None:
        model, row_iter = self.selected_row()
        return model[row_iter][COL_ID] if row_iter is not None else None

    def select_by_id(self, peer_id: str) -> None:
        for row in self.sorted:
            if row[COL_ID] == peer_id:
                self.tree.get_selection().select_iter(row.iter)
                return

    def on_selection_changed(self, _selection) -> None:
        _model, row_iter = self.selected_row()
        self.connect_button.set_sensitive(row_iter is not None)

    def on_button_press(self, _widget, event) -> bool:
        if event.button != 3:  # только правая кнопка
            return False

        path_info = self.tree.get_path_at_pos(int(event.x), int(event.y))
        if path_info is None:
            return False

        self.tree.get_selection().select_path(path_info[0])
        self.menu.popup_at_pointer(event)
        return True

    def connect_selected(self) -> None:
        model, row_iter = self.selected_row()
        if row_iter is None:
            return

        peer_id = model[row_iter][COL_ID]
        name = model[row_iter][COL_NAME]

        if not model[row_iter][COL_ONLINE] and not self.confirm_offline(name):
            return

        window = SessionWindow(self.api.web_client_url(peer_id), name, peer_id)
        window.connect("destroy", lambda w: self.sessions.remove(w) if w in self.sessions else None)
        self.sessions.append(window)
        window.show_all()

        self.status.set_text(f"Открываем сеанс с {name}…")

    def confirm_offline(self, name: str) -> bool:
        dialog = Gtk.MessageDialog(
            transient_for=self.app.window,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Устройство «{name}» сейчас не в сети.",
        )
        dialog.format_secondary_text("Всё равно попробовать подключиться?")
        answer = dialog.run()
        dialog.destroy()

        return answer == Gtk.ResponseType.YES

    def copy_id(self) -> None:
        model, row_iter = self.selected_row()
        if row_iter is None:
            return

        peer_id = model[row_iter][COL_ID]

        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(peer_id, -1)
        self.status.set_text(f"ID {peer_id} скопирован в буфер обмена.")

    def open_in_browser(self) -> None:
        model, row_iter = self.selected_row()
        if row_iter is None:
            return

        Gtk.show_uri_on_window(
            self.app.window,
            self.api.web_client_url(model[row_iter][COL_ID]),
            Gdk.CURRENT_TIME,
        )
