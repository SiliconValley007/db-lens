"""A deterministic, in-process stand-in for the parts of tkinter that db_lens.gui uses.

conftest installs it (``install()``) when no real Tk can be had: no ``$DISPLAY``, no ``Xvfb``, or ``tkinter.Tk()`` raising ``TclError``.
It is a behavioural model, not a mock of calls: widgets keep options, Treeview keeps rows, Text keeps its buffer (and ignores edits while
``state="disabled"``, like the real one), variables notify traces, ``after`` callbacks run from ``update()`` and the clipboard is shared.
Unknown *public* methods are harmless no-ops so layout-only calls never break a test; unknown options are stored and read back."""

from __future__ import annotations

import itertools
import sys
import time
from collections.abc import Callable
from types import ModuleType
from typing import Any

_CLIPBOARD = [""]
_TIMERS: list[tuple[float, str, Callable[..., Any], tuple[Any, ...]]] = []
_IDS = itertools.count(1)
SLACK = 0.05  # timers due within this many seconds run on the next update(): virtual time keeps chunked rendering deterministic


class TclError(Exception):
    """What real Tk raises for an impossible request (no display, duplicate Treeview item, ...)."""


class Event:
    x = y = x_root = y_root = 0

    def __class_getitem__(cls, item: Any) -> type[Event]:
        return cls


class Misc:
    def __init__(self, master: Any = None, **kw: Any) -> None:
        self.master = master
        self._opts: dict[str, Any] = dict(kw)
        self._children: list[Misc] = []
        self._alive = True
        self._bindings: dict[str, list[Callable[..., Any]]] = {}
        self._manager = ""
        if isinstance(master, Misc):
            master._children.append(self)

    # options -------------------------------------------------------------------------------------------------------------------
    def configure(self, cnf: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any] | None:
        merged = {**(cnf or {}), **kw}
        if not merged:
            return dict(self._opts)
        self._opts.update(merged)
        return None

    config = configure

    def cget(self, key: str) -> Any:
        return self._opts.get(key, "")

    def __getitem__(self, key: str) -> Any:
        return self.cget(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self._opts[key] = value

    def keys(self) -> list[str]:
        return list(self._opts)

    # geometry managers ---------------------------------------------------------------------------------------------------------
    def pack(self, **kw: Any) -> None:
        self._manager = "pack"

    def grid(self, **kw: Any) -> None:
        self._manager = "grid"

    def place(self, **kw: Any) -> None:
        self._manager = "place"

    def pack_forget(self) -> None:
        self._manager = ""

    def grid_remove(self) -> None:
        self._manager = ""

    grid_forget = grid_remove

    # introspection -------------------------------------------------------------------------------------------------------------
    def winfo_children(self) -> list[Misc]:
        return list(self._children)

    def winfo_exists(self) -> bool:
        return self._alive

    def winfo_width(self) -> int:
        return int(self._opts.get("width", 1) or 1)

    def winfo_height(self) -> int:
        return int(self._opts.get("height", 1) or 1)

    def winfo_ismapped(self) -> bool:
        return bool(self._manager)

    def winfo_manager(self) -> str:
        return self._manager

    def winfo_toplevel(self) -> Misc:
        node: Misc = self
        while isinstance(node.master, Misc):
            node = node.master
        return node

    def destroy(self) -> None:
        for child in list(self._children):
            child.destroy()
        self._alive = False
        if isinstance(self.master, Misc) and self in self.master._children:
            self.master._children.remove(self)

    # events, clipboard, timers -------------------------------------------------------------------------------------------------
    def bind(self, sequence: str | None = None, func: Callable[..., Any] | None = None, add: Any = None) -> str:
        if sequence and func:
            self._bindings.setdefault(sequence, []).append(func)
        return sequence or ""

    bind_all = bind

    def event_generate(self, sequence: str, **kw: Any) -> None:
        event = Event()
        for key, value in kw.items():
            setattr(event, key, value)
        for func in self._bindings.get(sequence, []):
            func(event)

    def clipboard_clear(self, **kw: Any) -> None:
        _CLIPBOARD[0] = ""

    def clipboard_append(self, string: str, **kw: Any) -> None:
        _CLIPBOARD[0] += string

    def clipboard_get(self, **kw: Any) -> str:
        return _CLIPBOARD[0]

    def after(self, ms: int | str, func: Callable[..., Any] | None = None, *args: Any) -> str:
        if func is None:
            time.sleep(int(ms) / 1000)
            return ""
        ident = f"after#{next(_IDS)}"
        _TIMERS.append((time.monotonic() + int(ms) / 1000, ident, func, args))
        return ident

    def after_cancel(self, ident: str) -> None:
        _TIMERS[:] = [t for t in _TIMERS if t[1] != ident]

    def update(self) -> None:
        horizon = time.monotonic() + SLACK
        due = sorted((t for t in _TIMERS if t[0] <= horizon), key=lambda t: t[0])
        for t in due:
            if t in _TIMERS:
                _TIMERS.remove(t)
                t[2](*t[3])

    def update_idletasks(self) -> None:
        return None

    def __getattr__(self, name: str) -> Callable[..., None]:
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: None  # layout-only calls (columnconfigure, focus_set, ...)


class Variable:
    _default: Any = ""

    def __init__(self, master: Any = None, value: Any = None, name: str | None = None) -> None:
        self._value = self._coerce(self._default if value is None else value)
        self._traces: list[Callable[..., Any]] = []

    def _coerce(self, value: Any) -> Any:
        return value

    def get(self) -> Any:
        return self._value

    def set(self, value: Any) -> None:
        self._value = self._coerce(value)
        for callback in list(self._traces):
            callback("var", "", "write")

    def trace_add(self, mode: Any, callback: Callable[..., Any]) -> str:
        self._traces.append(callback)
        return f"trace#{len(self._traces)}"


class StringVar(Variable):
    def _coerce(self, value: Any) -> str:
        return str(value)


class IntVar(Variable):
    _default = 0

    def _coerce(self, value: Any) -> int:
        return int(value)


class BooleanVar(Variable):
    _default = False

    def _coerce(self, value: Any) -> bool:
        return bool(value)


class Window(Misc):
    """Tk and Toplevel."""

    def __init__(self, master: Any = None, **kw: Any) -> None:
        super().__init__(master, **kw)
        self._title, self._geometry, self._quit = "", "200x200+0+0", False
        self._protocols: dict[str, Callable[..., Any]] = {}

    def title(self, string: str | None = None) -> str:
        if string is not None:
            self._title = string
        return self._title

    def geometry(self, newGeometry: str | None = None) -> str:
        if newGeometry is not None:
            self._geometry = newGeometry
        return self._geometry

    def protocol(self, name: str, func: Callable[..., Any]) -> None:
        self._protocols[name] = func

    def quit(self) -> None:
        self._quit = True

    def mainloop(self, n: int = 0) -> None:
        self._quit = False
        while not self._quit and self._alive:
            self.update()
            time.sleep(0.002)


class Tk(Window):
    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(None, **kw)


class Toplevel(Window):
    pass


class Frame(Misc):
    pass


class Label(Misc):
    pass


class Menu(Misc):
    def __init__(self, master: Any = None, **kw: Any) -> None:
        super().__init__(master, **kw)
        self.entries: list[dict[str, Any]] = []

    def add_command(self, **kw: Any) -> None:
        self.entries.append(kw)


class Text(Misc):
    """The buffer is only editable while state is not 'disabled' (as in real Tk); get() ends with the newline Tk always reports."""

    def __init__(self, master: Any = None, **kw: Any) -> None:
        super().__init__(master, **kw)
        self._buffer = ""

    def _editable(self) -> bool:
        return bool(self._opts.get("state", "normal") != "disabled")

    def insert(self, index: str, chars: str, *tags: Any) -> None:
        if self._editable():
            self._buffer = chars + self._buffer if index == "1.0" else self._buffer + chars

    def delete(self, index1: str, index2: str | None = None) -> None:
        if self._editable():
            self._buffer = ""

    def get(self, index1: str = "1.0", index2: str | None = None) -> str:
        return self._buffer + "\n"


class Button(Misc):
    def __init__(self, master: Any = None, **kw: Any) -> None:
        super().__init__(master, **kw)
        self._flags: set[str] = set()

    def state(self, spec: list[str] | None = None) -> tuple[str, ...]:
        for flag in spec or []:
            self._flags.discard(flag[1:]) if flag.startswith("!") else self._flags.add(flag)
        return tuple(sorted(self._flags))

    def instate(self, spec: list[str]) -> bool:
        return bool(all((f[1:] not in self._flags) if f.startswith("!") else (f in self._flags) for f in spec))

    def cget(self, key: str) -> Any:
        return ("disabled" if "disabled" in self._flags else "normal") if key == "state" else super().cget(key)

    def invoke(self) -> None:
        command = self._opts.get("command")
        if callable(command) and "disabled" not in self._flags:
            command()


class Checkbutton(Button):
    def invoke(self) -> None:
        var = self._opts.get("variable")
        if isinstance(var, Variable):
            var.set(not var.get())
        super().invoke()


class Combobox(Misc):
    def _var(self) -> Variable | None:
        var = self._opts.get("textvariable")
        return var if isinstance(var, Variable) else None

    def get(self) -> str:
        var = self._var()
        return str(var.get() if var else self._opts.get("_value", ""))

    def set(self, value: str) -> None:
        var = self._var()
        if var:
            var.set(value)
        else:
            self._opts["_value"] = value

    def cget(self, key: str) -> Any:
        return tuple(self._opts.get("values", ())) if key == "values" else super().cget(key)


class PanedWindow(Misc):
    def __init__(self, master: Any = None, **kw: Any) -> None:
        super().__init__(master, **kw)
        self._panes: list[Any] = []

    def add(self, child: Any, **kw: Any) -> None:
        self._panes.append(child)

    def forget(self, child: Any) -> None:
        self._panes.remove(child)

    def panes(self) -> tuple[Any, ...]:
        return tuple(self._panes)


class Treeview(Misc):
    def __init__(self, master: Any = None, **kw: Any) -> None:
        super().__init__(master, **kw)
        self._items: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._selected: list[str] = []
        self._headings: dict[str, dict[str, Any]] = {}
        self._columns: dict[str, dict[str, Any]] = {}

    def insert(self, parent: str, index: Any, iid: Any = None, **kw: Any) -> str:
        key = str(iid) if iid is not None else f"I{next(_IDS):03d}"
        if key in self._items:
            raise TclError(f"Item {key} already exists")
        self._items[key] = {"values": tuple(str(v) for v in kw.get("values", ())), "tags": tuple(kw.get("tags", ()))}
        self._order.append(key)
        return key

    def delete(self, *items: str) -> None:
        for key in items:
            self._items.pop(key, None)
            if key in self._order:
                self._order.remove(key)
            if key in self._selected:
                self._selected.remove(key)

    def get_children(self, item: str = "") -> tuple[str, ...]:
        return tuple(self._order)

    def item(self, iid: str, option: str | None = None, **kw: Any) -> Any:
        record = self._items[iid]
        if kw:
            record.update(kw)
            return None
        return record.get(option) if option else dict(record)

    def selection(self) -> tuple[str, ...]:
        return tuple(self._selected)

    def selection_set(self, *items: Any) -> None:
        flat: list[str] = []
        for entry in items:
            flat += [str(x) for x in entry] if isinstance(entry, (list, tuple)) else [str(entry)]
        self._selected = flat

    def _section(self, store: dict[str, dict[str, Any]], column: str, option: str | None, kw: dict[str, Any]) -> Any:
        record = store.setdefault(column, {})
        if kw:
            record.update(kw)
            return None
        return record.get(option, "") if option else dict(record)

    def heading(self, column: str, option: str | None = None, **kw: Any) -> Any:
        return self._section(self._headings, column, option, kw)

    def column(self, column: str, option: str | None = None, **kw: Any) -> Any:
        return self._section(self._columns, column, option, kw)

    def identify_row(self, y: int) -> str:
        return ""

    def identify_column(self, x: int) -> str:
        return "#1"


class Style:
    _theme = "default"

    def __init__(self, master: Any = None) -> None:
        self.master = master

    def theme_use(self, name: str | None = None) -> str:
        if name is not None:
            Style._theme = name
        return Style._theme

    def configure(self, *args: Any, **kw: Any) -> None:
        return None


class _Font:
    def measure(self, text: str) -> int:
        return 7 * len(text)


def _asksaveasfilename(**kw: Any) -> str:
    return ""


def build() -> dict[str, ModuleType]:
    """Fresh module objects named like the real ones; ``install()`` puts them in ``sys.modules``."""
    tk, ttk = ModuleType("tkinter"), ModuleType("tkinter.ttk")
    dialog, font = ModuleType("tkinter.filedialog"), ModuleType("tkinter.font")
    for name, obj in {
        "Tk": Tk, "Toplevel": Toplevel, "Frame": Frame, "Label": Label, "Text": Text, "Menu": Menu, "Misc": Misc, "Event": Event,
        "StringVar": StringVar, "IntVar": IntVar, "BooleanVar": BooleanVar, "TclError": TclError, "TkVersion": 8.6,
    }.items():  # fmt: skip
        setattr(tk, name, obj)
    for name, obj in {
        "Frame": Frame, "Label": Label, "Button": Button, "Checkbutton": Checkbutton, "Entry": Misc, "Spinbox": Misc, "Combobox": Combobox,
        "Separator": Misc, "Progressbar": Misc, "Scrollbar": Misc, "PanedWindow": PanedWindow, "Treeview": Treeview, "Style": Style,
    }.items():  # fmt: skip
        setattr(ttk, name, obj)
    dialog.asksaveasfilename = _asksaveasfilename  # type: ignore[attr-defined]
    font.nametofont = lambda name: _Font()  # type: ignore[attr-defined]
    tk.ttk, tk.filedialog, tk.font = ttk, dialog, font  # type: ignore[attr-defined]
    return {"tkinter": tk, "tkinter.ttk": ttk, "tkinter.filedialog": dialog, "tkinter.font": font}


def install() -> None:
    sys.modules.update(build())
