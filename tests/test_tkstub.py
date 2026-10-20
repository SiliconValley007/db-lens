"""The in-process Tk stand-in (tests/tkstub.py) behaves like the parts of Tk the GUI tests rely on."""

from __future__ import annotations

import sys
import time
from typing import Any

import pytest

from . import tkstub


@pytest.fixture
def tk() -> Any:
    mods = tkstub.build()
    return mods["tkinter"]


def test_widgets_keep_options_children_and_die_with_their_parent(tk: Any) -> None:
    root = tk.Tk()
    frame = tk.Frame(root, borderwidth=0)
    label = tk.Label(frame, text="hi")
    assert label.cget("text") == "hi" and label["text"] == "hi" and label.cget("never-set") == ""
    label.configure(text="yo", background="#fff")
    label["foreground"] = "#000"
    assert label.keys() == ["text", "background", "foreground"] and frame.configure() == {"borderwidth": 0}
    assert frame.winfo_children() == [label] and label.winfo_toplevel() is root
    frame.destroy()
    assert not label.winfo_exists() and root.winfo_children() == [] and root.winfo_exists()


def test_geometry_managers_and_sizes(tk: Any) -> None:
    w = tk.Frame(tk.Tk(), width=40)
    assert not w.winfo_ismapped() and w.winfo_width() == 40 and w.winfo_height() == 1
    for call, name in ((w.pack, "pack"), (w.grid, "grid"), (w.place, "place")):
        call()
        assert w.winfo_manager() == name and w.winfo_ismapped()
    w.pack_forget()
    assert not w.winfo_manager()
    w.grid()
    w.grid_remove()
    assert not w.winfo_ismapped()
    w.columnconfigure(0, weight=1)  # layout-only calls are harmless
    with pytest.raises(AttributeError):
        getattr(w, "_private_thing")  # noqa: B009


def test_after_runs_in_order_from_update_and_can_be_cancelled(tk: Any) -> None:
    root = tk.Tk()
    ran: list[str] = []
    root.after(20, ran.append, "b")
    root.after(1, ran.append, "a")
    gone = root.after(1, ran.append, "never")
    root.after_cancel(gone)
    root.after(10_000, ran.append, "far")  # not due: stays queued
    root.update()
    assert ran == ["a", "b"]
    t0 = time.monotonic()
    root.after(5)  # no callback: a plain sleep
    assert time.monotonic() - t0 >= 0.004
    tkstub._TIMERS.clear()


def test_mainloop_runs_until_quit_and_destroy_ends_it(tk: Any) -> None:
    root = tk.Tk()
    root.after(5, root.quit)
    root.mainloop()
    other = tk.Toplevel(root)
    other.after(5, other.destroy)
    other.mainloop()
    assert not other.winfo_exists()


def test_window_title_geometry_and_protocols(tk: Any) -> None:
    top = tk.Toplevel(tk.Tk())
    assert top.title() == "" and top.geometry() == "200x200+0+0"
    top.title("db-lens")
    top.geometry("1x1+2+3")
    top.protocol("WM_DELETE_WINDOW", top.destroy)
    assert (top.title(), top.geometry()) == ("db-lens", "1x1+2+3") and top._protocols["WM_DELETE_WINDOW"] == top.destroy


def test_variables_coerce_and_notify(tk: Any) -> None:
    seen: list[str] = []
    s, i, b = tk.StringVar(value=5), tk.IntVar(value="7"), tk.BooleanVar()
    s.trace_add("write", lambda *a: seen.append(s.get()))
    s.set("x")
    assert (s.get(), i.get(), b.get(), seen) == ("x", 7, False, ["x"])
    b.set(1)
    assert b.get() is True and tk.IntVar().get() == 0 and tk.StringVar().get() == ""


def test_text_ignores_edits_while_disabled_and_ends_with_a_newline(tk: Any) -> None:
    t = tk.Text(tk.Tk(), state="disabled")
    t.insert("end", "ignored")
    assert t.get("1.0", "end") == "\n"
    t.configure(state="normal")
    t.insert("end", "world")
    t.insert("1.0", "hello ")
    assert t.get("1.0", "end") == "hello world\n"
    t.delete("1.0", "end")
    assert t.get() == "\n"
    t.configure(state="disabled")
    t.delete("1.0", "end")  # also ignored while disabled
    t.configure(state="normal")
    t.insert("end", "x")
    t.configure(state="disabled")
    t.delete("1.0", "end")
    assert t.get() == "x\n"


def test_treeview_rows_selection_headings_and_columns(tk: Any) -> None:
    ttk = tkstub.build()["tkinter.ttk"]
    tv = ttk.Treeview(tk.Tk(), show="headings")
    tv["columns"] = ("c0",)
    assert tv["columns"] == ("c0",)
    tv.insert("", "end", iid="1", values=[5, "a"], tags=("odd",))
    tv.insert("", "end", values=["auto"])
    first, auto = tv.get_children()
    assert first == "1" and auto.startswith("I") and tv.item("1", "values") == ("5", "a") and tv.item("1", "tags") == ("odd",)
    with pytest.raises(tkstub.TclError, match="already exists"):
        tv.insert("", "end", iid="1")
    tv.item("1", tags=("even",))
    assert tv.item("1")["tags"] == ("even",)
    tv.selection_set(["1", auto])
    assert tv.selection() == ("1", auto)
    tv.selection_set("1")
    assert tv.selection() == ("1",)
    tv.heading("c0", text="Name", command=print)
    tv.column("c0", width=50, anchor="e")
    assert (
        tv.heading("c0", "text") == "Name"
        and tv.column("c0", "anchor") == "e"
        and tv.heading("c0")["text"] == "Name"
        and tv.column("zz", "width") == ""
    )
    assert tv.identify_row(3) == "" and tv.identify_column(3) == "#1"
    tv.delete("1")
    tv.delete("nope")
    assert tv.get_children() == (auto,) and tv.selection() == ()
    tv.delete(*tv.get_children())
    assert tv.get_children() == ()


def test_buttons_state_instate_invoke_and_checkbutton_toggle(tk: Any) -> None:
    ttk = tkstub.build()["tkinter.ttk"]
    clicks: list[int] = []
    b = ttk.Button(tk.Tk(), command=lambda: clicks.append(1))
    assert not b.instate(["disabled"]) and b.cget("state") == "normal"
    b.state(["disabled"])
    b.invoke()
    assert b.instate(["disabled"]) and b.cget("state") == "disabled" and clicks == [] and b.state() == ("disabled",)
    b.state(["!disabled"])
    b.invoke()
    assert b.instate(["!disabled"]) and clicks == [1]
    var, cb_clicks = tk.BooleanVar(), []
    c = ttk.Checkbutton(tk.Tk(), variable=var, command=lambda: cb_clicks.append(1))
    c.invoke()
    assert var.get() is True and cb_clicks == [1]


def test_combobox_values_and_value_with_and_without_a_variable(tk: Any) -> None:
    ttk = tkstub.build()["tkinter.ttk"]
    var = tk.StringVar()
    boxed = ttk.Combobox(tk.Tk(), textvariable=var, values=["a", "b"])
    boxed.set("b")
    assert var.get() == "b" and boxed.get() == "b" and boxed.cget("values") == ("a", "b")
    boxed.configure(values=["z"])
    assert boxed.cget("values") == ("z",)
    free = ttk.Combobox(tk.Tk())
    free.set("q")
    assert free.get() == "q" and free.cget("values") == ()


def test_paned_window_menu_style_font_dialog_and_clipboard(tk: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mods = tkstub.build()
    ttk, root = mods["tkinter.ttk"], tk.Tk()
    pw = ttk.PanedWindow(root)
    a, b = tk.Frame(pw), tk.Frame(pw)
    pw.add(a, weight=1)
    pw.add(b)
    pw.forget(a)
    assert pw.panes() == (b,)
    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="Copy")
    assert menu.entries == [{"label": "Copy"}]
    style = ttk.Style(root)
    assert style.theme_use("clam") == "clam" and ttk.Style(root).theme_use() == "clam" and style.configure(".", background="#fff") is None
    assert mods["tkinter.font"].nametofont("TkDefaultFont").measure("abc") == 21 and mods["tkinter.filedialog"].asksaveasfilename(parent=root) == ""
    root.clipboard_clear()
    root.clipboard_append("a")
    root.clipboard_append("b")
    assert tk.Tk().clipboard_get() == "ab"  # one clipboard for every window


def test_bindings_and_event_generate(tk: Any) -> None:
    w, got = tk.Frame(tk.Tk()), []
    assert w.bind("<Button-1>", lambda e: got.append((e.x, e.y))) == "<Button-1>" and w.bind("<Key>") == "<Key>"
    w.event_generate("<Button-1>", x=3, y=4)
    w.event_generate("<Nothing>")
    assert got == [(3, 4)] and tk.Event[tk.Misc] is tk.Event


def test_install_puts_the_stub_where_import_finds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.font"):
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name))  # restored on teardown
    tkstub.install()
    import importlib

    tkinter: Any = importlib.import_module("tkinter")
    ttk: Any = importlib.import_module("tkinter.ttk")
    filedialog: Any = importlib.import_module("tkinter.filedialog")
    font: Any = importlib.import_module("tkinter.font")

    assert (
        tkinter.Tk is tkstub.Tk
        and ttk.Treeview is tkstub.Treeview
        and tkinter.ttk is ttk
        and tkinter.filedialog is filedialog
        and tkinter.font is font
    )
