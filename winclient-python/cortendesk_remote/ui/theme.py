"""Светлая тема для ttk — та же палитра, что у веб-портала.

Тема ставится на 'clam': у стандартной темы Windows ('vista') половина
настроек цвета игнорируется, потому что она рисуется системными темами.
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

BG = "#f4f6fa"
CARD = "#ffffff"
LINE = "#dde2ea"
FG = "#1b1f2a"
MUT = "#5c6779"
ACC = "#2f5fe0"
ACC_HOVER = "#1f47b8"
OK = "#159a63"
OFF = "#a6b0be"
DANGER = "#d63a4e"
SOFT = "#eef1f6"
HOVER = "#f2f5fb"
SELECT = "#e6ecfb"

FONT_FAMILY = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
FONT = (FONT_FAMILY, 10)
FONT_BOLD = (FONT_FAMILY, 10, "bold")
FONT_H1 = (FONT_FAMILY, 14, "bold")
FONT_MONO = ("Consolas" if sys.platform == "win32" else "DejaVu Sans Mono", 10)


def apply(root: tk.Misc) -> ttk.Style:
    style = ttk.Style(root)
    style.theme_use("clam")

    # ВАЖНО: не ставить option_add("*Font", ...) — опция виджета перебивает
    # шрифт из ttk-стиля, и все заголовки схлопываются до размера основного
    # текста. Опция нужна только меню: tk.Menu не ttk-виджет и стиль не читает.
    root.option_add("*Menu.Font", FONT)
    root.option_add("*Menu.Background", CARD)
    root.option_add("*Menu.Foreground", FG)
    root.option_add("*Menu.activeBackground", HOVER)
    root.option_add("*Menu.activeForeground", FG)
    root.option_add("*Menu.relief", "solid")
    root.option_add("*Menu.borderWidth", 1)

    style.configure(".", background=BG, foreground=FG, font=FONT)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Card.TLabel", background=CARD, foreground=FG)
    style.configure("Muted.TLabel", background=BG, foreground=MUT)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUT)
    style.configure("H1.TLabel", background=CARD, foreground=FG, font=FONT_H1)
    style.configure("Error.TLabel", background=CARD, foreground=DANGER)

    style.configure(
        "TEntry",
        fieldbackground=CARD,
        foreground=FG,
        bordercolor=LINE,
        lightcolor=LINE,
        darkcolor=LINE,
        padding=5,
    )
    style.map("TEntry", bordercolor=[("focus", ACC)])

    style.configure("TCheckbutton", background=BG, foreground=FG)
    style.configure("Card.TCheckbutton", background=CARD, foreground=FG)

    style.configure(
        "TCombobox",
        fieldbackground=CARD,
        background=CARD,
        foreground=FG,
        bordercolor=LINE,
        lightcolor=LINE,
        darkcolor=LINE,
        arrowcolor=MUT,
        padding=4,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", CARD)],
        background=[("readonly", CARD), ("active", HOVER)],
        bordercolor=[("focus", ACC)],
    )
    # Выпадающий список — это tk.Listbox внутри комбобокса, стилю он не подчиняется.
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", SELECT)
    root.option_add("*TCombobox*Listbox.selectForeground", FG)

    style.configure("TCheckbutton", indicatorcolor=CARD)
    style.map("TCheckbutton", indicatorcolor=[("selected", ACC)])

    style.configure("Vertical.TScrollbar", background=SOFT, troughcolor=BG,
                    bordercolor=LINE, arrowcolor=MUT)

    # Кнопки: основная синяя и вторичная серая.
    style.configure(
        "Accent.TButton",
        background=ACC,
        foreground="#ffffff",
        bordercolor=ACC,
        focuscolor=ACC,
        padding=(14, 6),
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", OFF), ("active", ACC_HOVER)],
        bordercolor=[("disabled", OFF), ("active", ACC_HOVER)],
    )

    style.configure(
        "Secondary.TButton",
        background=SOFT,
        foreground=FG,
        bordercolor=LINE,
        focuscolor=SOFT,
        padding=(12, 5),
    )
    style.map("Secondary.TButton", background=[("active", HOVER), ("disabled", SOFT)])

    style.configure(
        "Treeview",
        background=CARD,
        fieldbackground=CARD,
        foreground=FG,
        bordercolor=LINE,
        rowheight=26,
    )
    style.configure(
        "Treeview.Heading",
        background=SOFT,
        foreground=MUT,
        font=FONT_BOLD,
        padding=(8, 6),
        relief="flat",
    )
    style.map("Treeview.Heading", background=[("active", HOVER)])
    style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", FG)])

    return style
