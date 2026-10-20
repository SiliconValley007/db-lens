"""Reusable widgets: result grid, live activity log, banners, status bar."""

from __future__ import annotations

import logging
import time
import tkinter as tk
from collections import deque
from collections.abc import Callable
from decimal import Decimal
from functools import partial
from tkinter import filedialog, ttk
from typing import Any

from .. import cli, service
from .theme import Theme

NULL_TEXT = "‹null›"


def human_bytes(n: float | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_int(n: int | None, approx: bool = False) -> str:
    return "?" if n is None else f"{'~' if approx else ''}{n:,}"


class DataGrid(ttk.Frame):
    """Sortable, scrollable result grid. Rows are inserted in small chunks so large results stay responsive."""

    CHUNK = 300

    def __init__(self, master: tk.Misc, theme: Theme, max_cell: int = 300, on_rendered: Callable[[int], None] | None = None) -> None:
        super().__init__(master)
        self.theme, self.max_cell, self.on_rendered = theme, max_cell, on_rendered
        self.tree = ttk.Treeview(self, show="headings", selectmode="extended")
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.columns: list[str] = []
        self.rows: list[list[Any]] = []
        self._order: list[int] = []
        self._gen = 0
        self._sort: tuple[int, bool] | None = None
        self._cell: tuple[str, str] | None = None
        self.formatters: dict[int, Callable[[Any], str]] = {}  # column index -> display text (sorting still uses the raw value)
        self.menu = tk.Menu(self.tree, tearoff=0)
        self.menu.add_command(label="Copy cell", command=self.copy_cell)
        self.menu.add_command(label="Copy selected rows", command=self.copy_selected, accelerator="Ctrl+C")
        self.menu.add_command(label="Copy all rows with header", command=self.copy_all)
        for seq in ("<Button-3>", "<Button-2>"):
            self.tree.bind(seq, self._popup)
        self.tree.bind("<Control-c>", self._on_copy)
        self.tree.bind("<Control-a>", self._on_select_all)
        self.restyle()

    def restyle(self) -> None:
        self.tree.tag_configure("odd", background=self.theme.c["stripe"])
        self.tree.tag_configure("even", background="")

    # -- data --------------------------------------------------------------------------------------------------------------------
    def clear(self) -> None:
        self._gen += 1
        self.columns, self.rows, self._order, self._sort = [], [], [], None
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = ()

    def set_data(self, columns: list[str], rows: list[list[Any]], formatters: dict[int, Callable[[Any], str]] | None = None) -> None:
        self._gen += 1
        self.formatters = formatters or {}
        self.columns, self.rows, self._sort = list(columns), [list(r) for r in rows], None
        self._order = list(range(len(self.rows)))
        self._build_columns()
        self._render()

    def _build_columns(self) -> None:
        from tkinter import font as tkfont

        font = tkfont.nametofont("TkDefaultFont")
        ids = [f"c{i}" for i in range(len(self.columns))]
        self.tree["columns"] = ids
        for i, (cid, name) in enumerate(zip(ids, self.columns)):
            sample = [self._text(r[i], i) for r in self.rows[:150]]
            width = max([font.measure(cli.clean(name)) + 34] + [font.measure(s[:60]) + 22 for s in sample] + [70])
            numeric = bool(self.rows) and all(isinstance(r[i], (int, float, Decimal)) or r[i] is None for r in self.rows[:150])
            self.tree.heading(cid, text=cli.clean(name), command=partial(self.sort_by, i))
            self.tree.column(cid, width=min(width, 360), minwidth=50, anchor="e" if numeric else "w", stretch=False)

    def _text(self, v: Any, i: int = -1) -> str:
        if i in self.formatters:
            return self.formatters[i](v)
        return NULL_TEXT if v is None else service.display(v, self.max_cell).replace("\n", " ⏎ ")

    def _render(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._gen += 1
        self._insert(0, self._gen)

    def _insert(self, start: int, gen: int) -> None:
        if gen != self._gen:
            return
        end = min(start + self.CHUNK, len(self._order))
        for k in range(start, end):
            idx = self._order[k]
            self.tree.insert(
                "", "end", iid=str(idx), values=[self._text(v, j) for j, v in enumerate(self.rows[idx])], tags=("odd" if k % 2 else "even",)
            )
        if end < len(self._order):
            self.after(1, lambda: self._insert(end, gen))
        elif self.on_rendered:
            self.on_rendered(len(self._order))

    @property
    def rendering(self) -> bool:
        return len(self.tree.get_children()) < len(self._order)

    # -- sorting -----------------------------------------------------------------------------------------------------------------
    def sort_by(self, i: int) -> None:
        desc = bool(self._sort and self._sort[0] == i and not self._sort[1])
        self._sort = (i, desc)

        def key(idx: int) -> tuple[bool, int, Any]:
            v = self.rows[idx][i]
            if v is None:
                return (True, 0, 0)
            if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
                return (False, 0, float(v))
            return (False, 1, str(v).lower())

        nulls = [x for x in self._order if self.rows[x][i] is None]
        rest = sorted((x for x in self._order if self.rows[x][i] is not None), key=key, reverse=desc)
        self._order = rest + nulls
        for j, name in enumerate(self.columns):
            arrow = (" ▼" if desc else " ▲") if j == i else ""
            self.tree.heading(f"c{j}", text=cli.clean(name) + arrow)
        self._render()

    # -- copy --------------------------------------------------------------------------------------------------------------------
    def copy_selected(self) -> None:
        self.copy_rows(False)

    def copy_all(self) -> None:
        self.copy_rows(True)

    def _on_copy(self, _event: tk.Event[tk.Misc]) -> str:
        self.copy_rows(False)
        return "break"

    def _on_select_all(self, _event: tk.Event[tk.Misc]) -> str:
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _popup(self, event: tk.Event[tk.Misc]) -> None:
        row, col = self.tree.identify_row(event.y), self.tree.identify_column(event.x)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self._cell = (row, col)
            self.menu.tk_popup(event.x_root, event.y_root)

    def _plain(self, v: Any) -> str:
        return "" if v is None else str(v)

    def copy_cell(self) -> None:
        if self._cell:
            row, col = self._cell
            self._clip(self._plain(self.rows[int(row)][int(col[1:]) - 1]))

    def copy_rows(self, all_rows: bool) -> None:
        idxs = [int(i) for i in (self._order if all_rows else self.tree.selection())]
        if not all_rows:
            order = {x: k for k, x in enumerate(self._order)}
            idxs.sort(key=lambda x: order.get(x, 0))
        lines = (["\t".join(self.columns)] if all_rows else []) + ["\t".join(self._plain(v) for v in self.rows[i]) for i in idxs]
        self._clip("\n".join(lines))

    def _clip(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    def export(self, parent: tk.Misc, fmt: str, name: str = "results") -> str | None:
        """Ask for a file and write the rows (sorted as shown). Returns the path or None."""
        if not self.columns:
            return None
        path = filedialog.asksaveasfilename(
            parent=parent, defaultextension=f".{fmt}", initialfile=f"{name}.{fmt}", filetypes=[(fmt.upper(), f"*.{fmt}"), ("All files", "*.*")]
        )
        if not path:
            return None
        service.export_rows(path, fmt, self.columns, [self.rows[i] for i in self._order])
        return str(path)


class LogPane(ttk.Frame):
    """Live activity feed: what db-lens is doing right now, in plain words (details on demand)."""

    MAX = 5000

    def __init__(self, master: tk.Misc, theme: Theme) -> None:
        super().__init__(master)
        self.theme = theme
        self.entries: deque[tuple[float, int, str]] = deque(maxlen=self.MAX)
        bar = ttk.Frame(self)
        ttk.Label(bar, text="Activity", font=("TkDefaultFont", 10, "bold")).pack(side="left", padx=(8, 12))
        self.details = tk.BooleanVar(value=False)
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Technical details", variable=self.details, command=self.render_all).pack(side="left", padx=4)
        ttk.Checkbutton(bar, text="Follow", variable=self.follow).pack(side="left", padx=4)
        for text, cmd in (("Save…", self.save), ("Copy", self.copy), ("Clear", self.clear)):
            ttk.Button(bar, text=text, command=cmd).pack(side="right", padx=3, pady=2)
        bar.pack(fill="x")
        body = ttk.Frame(self)
        self.text = tk.Text(body, height=8, wrap="word", state="disabled", borderwidth=0, highlightthickness=0, font=Theme.mono())
        sb = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        body.pack(fill="both", expand=True)
        self.restyle()

    def restyle(self) -> None:
        c = self.theme.c
        self.text.configure(background=c["editor"], foreground=c["fg"], insertbackground=c["fg"], selectbackground=c["sel"])
        self.text.tag_configure("ts", foreground=c["muted"])
        self.text.tag_configure("info", foreground=c["fg"])
        self.text.tag_configure("debug", foreground=c["muted"])
        self.text.tag_configure("warning", foreground=c["warn"])
        self.text.tag_configure("error", foreground=c["err"], font=("TkFixedFont", 10, "bold"))
        self.render_all()

    def add(self, created: float, level: int, msg: str) -> None:
        self.entries.append((created, level, msg))
        if level >= logging.INFO or self.details.get():
            self._write(created, level, msg)

    def _write(self, created: float, level: int, msg: str) -> None:
        tag = "error" if level >= logging.ERROR else "warning" if level >= logging.WARNING else "info" if level >= logging.INFO else "debug"
        self.text.configure(state="normal")
        self.text.insert("end", time.strftime("%H:%M:%S ", time.localtime(created)), "ts")
        self.text.insert("end", ("⚠ " if tag == "warning" else "✖ " if tag == "error" else "· " if tag == "debug" else "▸ ") + msg + "\n", tag)
        self.text.configure(state="disabled")
        if self.follow.get():
            self.text.see("end")

    def render_all(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        for created, level, msg in self.entries:
            if level >= logging.INFO or self.details.get():
                self._write(created, level, msg)

    def contents(self) -> str:
        return str(self.text.get("1.0", "end")).rstrip()

    def clear(self) -> None:
        self.entries.clear()
        self.render_all()

    def copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.contents())

    def save(self) -> None:
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".log", initialfile="db-lens-activity.log")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.contents() + "\n")


class Banner(tk.Frame):
    """A dismissible coloured message strip (info / ok / warn / err)."""

    def __init__(self, master: tk.Misc, theme: Theme) -> None:
        super().__init__(master, borderwidth=0)
        self.theme, self.kind = theme, "info"
        self.label = tk.Label(self, anchor="w", justify="left", padx=10, pady=6, wraplength=900, font=("TkDefaultFont", 10))
        self.label.pack(side="left", fill="x", expand=True)
        self.close = tk.Label(self, text="✕", padx=10, cursor="hand2")
        self.close.pack(side="right")
        self.close.bind("<Button-1>", self._dismiss)
        self.bind("<Configure>", self._rewrap)
        self.visible = False

    def _dismiss(self, _event: tk.Event[tk.Misc]) -> None:
        self.hide()

    def _rewrap(self, _event: tk.Event[tk.Misc]) -> None:
        self.label.configure(wraplength=max(200, self.winfo_width() - 70))

    def show(self, kind: str, text: str) -> None:
        self.kind = kind
        self.restyle()
        self.label.configure(text=text)
        self.visible = True
        self.grid()

    def restyle(self) -> None:
        c = self.theme.c
        bg, fg = c[f"bg_{self.kind}"], c["err" if self.kind == "err" else "warn" if self.kind == "warn" else "ok" if self.kind == "ok" else "info"]
        for w in (self, self.label, self.close):
            w.configure(background=bg)
        self.label.configure(foreground=fg)
        self.close.configure(foreground=fg)

    def hide(self) -> None:
        self.visible = False
        self.grid_remove()


class SearchEntry(ttk.Frame):
    """Entry with a clear button; ``var`` holds the real text."""

    def __init__(self, master: tk.Misc, placeholder: str, on_change: Callable[[str], None]) -> None:
        super().__init__(master)
        self.placeholder, self.on_change = placeholder, on_change
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, width=28)
        self.entry.pack(side="left", fill="x", expand=True)
        ttk.Button(self, text="✕", width=2, command=lambda: self.set("")).pack(side="left", padx=(2, 0))
        self.var.trace_add("write", lambda *a: self.on_change(self.var.get()))

    def set(self, value: str) -> None:
        self.var.set(value)

    def focus(self) -> None:
        self.entry.focus_set()


class StatusBar(ttk.Frame):
    def __init__(self, master: tk.Misc, theme: Theme) -> None:
        super().__init__(master)
        self.theme = theme
        ttk.Separator(self).pack(side="top", fill="x")
        row = ttk.Frame(self)
        row.pack(fill="x", padx=8, pady=3)
        self.op = ttk.Label(row, text="Ready", anchor="w")
        self.op.pack(side="left")
        self.bar = ttk.Progressbar(row, mode="indeterminate", length=130)
        self.elapsed = ttk.Label(row, text="", width=7)
        self.badges = ttk.Frame(row)
        self.badges.pack(side="right")
        self.busy = False

    def set_busy(self, label: str, seconds: float) -> None:
        if not self.busy:
            self.busy = True
            self.bar.pack(side="left", padx=(12, 6))
            self.elapsed.pack(side="left")
            self.bar.start(12)
        self.op.configure(text=label)
        self.elapsed.configure(text=f"{seconds:.1f}s")

    def set_idle(self, text: str = "Ready") -> None:
        if self.busy:
            self.busy = False
            self.bar.stop()
            self.bar.pack_forget()
            self.elapsed.pack_forget()
        self.op.configure(text=text)

    def set_badges(self, items: list[tuple[str, str]]) -> None:
        for w in self.badges.winfo_children():
            w.destroy()
        c = self.theme.c
        for text, kind in items:
            fg = c[kind if kind in ("err", "warn", "ok") else "info"]
            tk.Label(
                self.badges, text=f" {text} ", background=c[f"bg_{kind}"], foreground=fg, font=("TkDefaultFont", 9, "bold"), padx=3, pady=1
            ).pack(side="left", padx=3)
