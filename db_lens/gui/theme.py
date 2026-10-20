"""Colour palettes and fonts for the GUI widgets. Pure data plus one optional call that styles ttk (sv-ttk when installed)."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#fafafa",
        "fg": "#1c1c1c",
        "muted": "#6b6b6b",
        "editor": "#ffffff",
        "sel": "#cfe3ff",
        "stripe": "#f1f4f8",
        "info": "#0b5cad",
        "ok": "#1b7a3a",
        "warn": "#8a5a00",
        "err": "#b3261e",
        "bg_info": "#e3effc",
        "bg_ok": "#e2f4e7",
        "bg_warn": "#fdf1d6",
        "bg_err": "#fbe4e2",
    },
    "dark": {
        "bg": "#1e1e1e",
        "fg": "#e6e6e6",
        "muted": "#9a9a9a",
        "editor": "#252526",
        "sel": "#264f78",
        "stripe": "#262a2f",
        "info": "#6cb2ff",
        "ok": "#6fd08c",
        "warn": "#f0c060",
        "err": "#ff8a80",
        "bg_info": "#1b2c3f",
        "bg_ok": "#1c3326",
        "bg_warn": "#3a2f14",
        "bg_err": "#3d1f1d",
    },
}
DEFAULT = "light"


class Theme:
    """``c`` is the active palette (colour name -> hex). Unknown theme names fall back to light."""

    def __init__(self, name: str = DEFAULT) -> None:
        self.name = DEFAULT
        self.c: dict[str, str] = dict(PALETTES[DEFAULT])
        self.set(name)

    def set(self, name: str) -> None:
        self.name = name if name in PALETTES else DEFAULT
        self.c = dict(PALETTES[self.name])

    @staticmethod
    def mono() -> tuple[str, int]:
        family = "Consolas" if sys.platform == "win32" else "Menlo" if sys.platform == "darwin" else "DejaVu Sans Mono"
        return (family, 10)

    def apply(self, root: tk.Misc) -> None:
        """Style ttk: sv-ttk when installed, otherwise the built-in ``clam`` theme with this palette."""
        try:
            import sv_ttk

            sv_ttk.set_theme(self.name, root)
        except ImportError:
            style = ttk.Style(root)
            style.theme_use("clam")
            style.configure(".", background=self.c["bg"], foreground=self.c["fg"])
            style.configure("Treeview", background=self.c["editor"], fieldbackground=self.c["editor"], foreground=self.c["fg"])
