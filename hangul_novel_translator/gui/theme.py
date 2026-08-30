# hangul_novel_translator/gui/theme.py
from __future__ import annotations

from tkinter import ttk


# ---------------- Theme & UI Style Configuration ----------------
THEME = {
    "bg": "#0D1117",
    "card": "#161B22",
    "card_alt": "#1C2128",
    "card_border": "#30363D",
    "input_bg": "#0D1117",
    "accent": "#238636",
    "accent_hover": "#2EA043",
    "primary": "#1F6FEB",
    "primary_hover": "#388BFD",
    "danger": "#DA3633",
    "danger_hover": "#F85149",
    "secondary": "#21262D",
    "secondary_hover": "#30363D",
    "text_main": "#F0F6FC",
    "text_muted": "#8B949E",
    "text_subtle": "#6E7681",
}


def _apply_ttk_theme(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(
        "Custom.Treeview",
        background=THEME["card"],
        foreground="#E6EDF3",
        fieldbackground=THEME["card"],
        rowheight=32,
        font=("Microsoft YaHei UI", 11),
        borderwidth=0,
        relief="flat",
    )
    style.map(
        "Custom.Treeview",
        background=[("selected", "#1F6FEB")],
        foreground=[("selected", "#FFFFFF")],
    )
    style.configure(
        "Custom.Treeview.Heading",
        background="#21262D",
        foreground="#C9D1D9",
        font=("Microsoft YaHei UI", 11, "bold"),
        relief="flat",
        padding=(8, 5),
    )
    style.map(
        "Custom.Treeview.Heading",
        background=[("active", "#30363D")],
        foreground=[("active", "#58A6FF")],
    )
