"""Список устройств: таблица с присутствием, поиск, фильтры, сортировка.

Присутствие на сервере обновляется примерно раз в 15 секунд (heartbeat
клиентов), поэтому и опрос идёт с тем же шагом — чаще смысла нет.
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from .. import launcher, store
from . import theme
from .tasks import run_async

#: (ключ, заголовок, ширина, растягивать ли)
COLUMNS = (
    ("status", "", 34, False),
    ("device_name", "Устройство", 220, True),
    ("id", "ID", 110, False),
    ("os", "ОС", 110, False),
    ("local_user", "Пользователь", 140, True),
    ("owner", "Владелец", 130, True),
    ("group", "Папка", 130, True),
    ("note", "Заметка", 200, True),
)

SORTABLE = {key for key, _title, _width, _stretch in COLUMNS} - {"status"}


class DevicesFrame(ttk.Frame):
    def __init__(self, master, app) -> None:
        super().__init__(master, style="TFrame")

        self.app = app
        self.api = app.api
        self.settings = app.settings

        self.peers: list[dict] = []
        self.rows: list[dict] = []          # то, что сейчас в таблице, в порядке показа
        self.sort_key = "device_name"
        self.sort_asc = True
        self.refreshing = False
        self.timer: str | None = None

        self._build_header()
        self._build_filters()
        self._build_table()
        self._build_status()

        self.load_groups()
        self.refresh(silent=False)
        self.schedule_refresh()

    # ---- разметка ------------------------------------------------------

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=theme.CARD, highlightbackground=theme.LINE, highlightthickness=1)
        header.pack(fill="x")

        left = ttk.Frame(header, style="Card.TFrame")
        left.pack(side="left", padx=16, pady=9)

        ttk.Label(left, text="Устройства", style="H1.TLabel").pack(side="left")
        ttk.Label(left, text=self.api.host, style="CardMuted.TLabel").pack(side="left", padx=(14, 0))

        right = ttk.Frame(header, style="Card.TFrame")
        right.pack(side="right", padx=16, pady=9)

        user = self.app.user or {}
        title = user.get("display_name") or user.get("name") or ""
        if user.get("is_admin"):
            title = f"{title} · администратор"

        ttk.Label(right, text=title, style="CardMuted.TLabel").pack(side="left", padx=(0, 12))
        ttk.Button(right, text="Выйти", style="Secondary.TButton",
                   command=self.app.logout).pack(side="left")

    def _build_filters(self) -> None:
        bar = ttk.Frame(self, style="TFrame", padding=(16, 10))
        bar.pack(fill="x")

        ttk.Label(bar, text="Поиск").pack(side="left", padx=(0, 8))

        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.render())
        entry = ttk.Entry(bar, textvariable=self.search, width=32)
        entry.pack(side="left")

        ttk.Label(bar, text="Папка").pack(side="left", padx=(18, 8))

        self.group = tk.StringVar(value="Все папки")
        self.group_box = ttk.Combobox(bar, textvariable=self.group, state="readonly",
                                      values=["Все папки"], width=22)
        self.group_box.pack(side="left")
        self.group_box.bind("<<ComboboxSelected>>", lambda _event: self.render())

        self.online_only = tk.BooleanVar(value=bool(self.settings["online_only"]))
        ttk.Checkbutton(bar, text="Только в сети", variable=self.online_only,
                        command=self.on_online_only).pack(side="left", padx=(18, 0))

        ttk.Button(bar, text="Обновить", style="Secondary.TButton",
                   command=lambda: self.refresh(silent=False)).pack(side="left", padx=(18, 0))

        self.connect_button = ttk.Button(bar, text="Подключиться", style="Accent.TButton",
                                         command=self.connect_selected, state="disabled")
        self.connect_button.pack(side="right")

    def _build_table(self) -> None:
        wrap = tk.Frame(self, bg=theme.CARD, highlightbackground=theme.LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        keys = [key for key, _title, _width, _stretch in COLUMNS]

        self.tree = ttk.Treeview(wrap, columns=keys, show="headings", selectmode="browse")

        for key, title, width, stretch in COLUMNS:
            self.tree.heading(
                key,
                text=title,
                anchor="center" if key == "status" else "w",
                command=(lambda k=key: self.sort_by(k)) if key in SORTABLE else "",
            )
            self.tree.column(key, width=width, stretch=stretch,
                             anchor="center" if key == "status" else "w")

        # Цвет точки присутствия задаётся тегом строки: в Treeview нельзя
        # покрасить одну ячейку, но тут красится только колонка статуса.
        self.tree.tag_configure("online", foreground=theme.FG)
        self.tree.tag_configure("offline", foreground=theme.MUT)

        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", lambda _event: self.connect_selected())
        self.tree.bind("<Return>", lambda _event: self.connect_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.on_select())
        self.tree.bind("<Button-3>", self.on_right_click)

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Подключиться", command=self.connect_selected)
        self.menu.add_command(label="Копировать ID", command=self.copy_id)
        self.menu.add_separator()
        self.menu.add_command(label="Открыть в браузере", command=self.open_in_browser)

    def _build_status(self) -> None:
        bar = tk.Frame(self, bg=theme.CARD, highlightbackground=theme.LINE, highlightthickness=1)
        bar.pack(fill="x")

        self.status = ttk.Label(bar, text="", style="CardMuted.TLabel")
        self.status.pack(anchor="w", padx=16, pady=6)

    # ---- данные --------------------------------------------------------

    def load_groups(self) -> None:
        def done(ok: bool, result) -> None:
            # Прав на список папок может не быть — это не повод ломать экран.
            values = ["Все папки"] + (result if ok else [])
            self.group_box.configure(values=values)

        run_async(self, self.api.device_groups, done)

    def refresh(self, silent: bool) -> None:
        if self.refreshing:
            return

        self.refreshing = True
        if not silent:
            self.status.configure(text="Загружаем список устройств…")

        def done(ok: bool, result) -> None:
            self.refreshing = False

            if ok:
                self.peers = [_flatten(peer) for peer in result]
                self.render()

                online = sum(1 for peer in self.peers if peer["online"])
                now = datetime.now().strftime("%H:%M:%S")
                self.status.configure(
                    text=f"Устройств: {len(self.peers)} · в сети: {online} · обновлено в {now}"
                )
                return

            if getattr(result, "expired", False):
                self.app.on_auth_expired()
                return

            # Обрыв связи не должен глушить автообновление: следующая попытка
            # придёт по таймеру.
            self.status.configure(text=f"Не удалось обновить список: {result.message}")

        run_async(self, self.api.peers, done)

    def schedule_refresh(self) -> None:
        seconds = int(self.settings["auto_refresh_seconds"])
        self.timer = self.after(seconds * 1000, self._tick)

    def _tick(self) -> None:
        self.refresh(silent=True)
        self.schedule_refresh()

    def stop(self) -> None:
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None

    # ---- отрисовка -----------------------------------------------------

    def visible_peers(self) -> list[dict]:
        needle = self.search.get().strip().lower()
        group = self.group.get()
        online_only = self.online_only.get()

        def matches(peer: dict) -> bool:
            if online_only and not peer["online"]:
                return False
            if group and group != "Все папки" and peer["group"] != group:
                return False
            if not needle:
                return True

            return any(
                needle in str(peer[key]).lower()
                for key in ("id", "device_name", "os", "local_user", "owner", "group", "note")
            )

        rows = [peer for peer in self.peers if matches(peer)]
        rows.sort(key=lambda peer: str(peer[self.sort_key]).lower(), reverse=not self.sort_asc)
        return rows

    def render(self) -> None:
        selected = self.selected_peer()
        selected_id = selected["id"] if selected else None

        self.tree.delete(*self.tree.get_children())
        self.rows = self.visible_peers()

        for peer in self.rows:
            self.tree.insert(
                "",
                "end",
                iid=peer["id"],
                values=(
                    "●" if peer["online"] else "○",
                    peer["device_name"],
                    peer["id"],
                    peer["os"],
                    peer["local_user"],
                    peer["owner"],
                    peer["group"],
                    peer["note"],
                ),
                tags=("online",) if peer["online"] else ("offline",),
            )

        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)

        self.on_select()

    def sort_by(self, key: str) -> None:
        self.sort_asc = not self.sort_asc if self.sort_key == key else True
        self.sort_key = key
        self.render()

    # ---- действия ------------------------------------------------------

    def selected_peer(self) -> dict | None:
        selection = self.tree.selection()
        if not selection:
            return None

        return next((peer for peer in self.peers if peer["id"] == selection[0]), None)

    def on_select(self) -> None:
        self.connect_button.configure(state="normal" if self.tree.selection() else "disabled")

    def on_online_only(self) -> None:
        self.settings["online_only"] = bool(self.online_only.get())
        store.save_settings(self.settings)
        self.render()

    def on_right_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return

        self.tree.selection_set(row)
        self.menu.tk_popup(event.x_root, event.y_root)
        self.menu.grab_release()

    def connect_selected(self) -> None:
        peer = self.selected_peer()
        if not peer:
            return

        if not peer["online"]:
            confirmed = messagebox.askyesno(
                "CortenDesk Remote",
                f"Устройство «{peer['device_name']}» сейчас не в сети.\n"
                "Всё равно попробовать подключиться?",
                parent=self,
            )
            if not confirmed:
                return

        url = self.api.web_client_url(peer["id"])
        title = f"{peer['device_name']} ({peer['id']}) — CortenDesk Remote"

        embedded, message = launcher.open_session(url, title)
        self.status.configure(
            text=message if not embedded else f"Открываем сеанс с {peer['device_name']}…"
        )

    def copy_id(self) -> None:
        peer = self.selected_peer()
        if not peer:
            return

        self.clipboard_clear()
        self.clipboard_append(peer["id"])
        self.status.configure(text=f"ID {peer['id']} скопирован в буфер обмена.")

    def open_in_browser(self) -> None:
        import webbrowser

        peer = self.selected_peer()
        if peer:
            webbrowser.open(self.api.web_client_url(peer["id"]))


def _flatten(peer: dict) -> dict:
    """PeerPayload из /api/peers -> плоская строка таблицы."""
    info = peer.get("info") or {}

    return {
        "id": peer.get("id") or "",
        "device_name": info.get("device_name") or peer.get("id") or "",
        "os": info.get("os") or "",
        "local_user": info.get("username") or "",
        "owner": peer.get("user_name") or peer.get("user") or "",
        "group": peer.get("device_group_name") or "",
        "note": peer.get("note") or "",
        "online": peer.get("status") == 1,
    }
