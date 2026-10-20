"""GUI building blocks (db_lens.gui) and the presentation helpers they use (db_lens.service). Real Tk widgets on a hidden root."""

from __future__ import annotations

import json
import logging
import sys
import tkinter as tk
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from db_lens import service, store
from db_lens.gui import widgets
from db_lens.gui.settings import DEFAULTS, MAX_HISTORY, Settings
from db_lens.gui.theme import PALETTES, Theme
from db_lens.gui.widgets import Banner, DataGrid, LogPane, SearchEntry, StatusBar, fmt_int, human_bytes

from .fakes import assert_mode


@pytest.fixture
def frame(tk_root: tk.Tk) -> Iterator[tk.Frame]:
    f = tk.Frame(tk_root)
    yield f
    f.destroy()


def peek(obj: object, name: str) -> Any:
    """Read an attribute without mypy narrowing it from an earlier assert (widgets change state behind its back)."""
    return getattr(obj, name)


def pump(root: tk.Misc, grid: DataGrid | None = None) -> None:
    for _ in range(200):
        root.update()
        if grid is None or not grid.rendering:
            return


def saveas(monkeypatch: pytest.MonkeyPatch, result: str) -> None:
    monkeypatch.setattr("tkinter.filedialog.asksaveasfilename", lambda **_k: result)


# ------------------------------------------------------------------------------------------------------------ service
def test_display_cleans_and_truncates() -> None:
    assert service.display("a\x1b[31mb") == "a\\x1b[31mb"
    assert service.display("x" * 10, 5) == "xxxx…"
    assert service.display("short", 5) == "short"
    assert service.display(7, 1) == "7"


def test_export_csv_neutralises_formulas_and_is_private(tmp_path: Path) -> None:
    out = tmp_path / "r.csv"
    service.export_rows(out, "csv", ["=bad", "n"], [["=1+1", 5], [None, "ok"]])
    assert out.read_text(encoding="utf-8").splitlines() == ["'=bad,n", "'=1+1,5", ",ok"]
    assert_mode(out, 0o600)
    assert [p.name for p in tmp_path.iterdir()] == ["r.csv"]


def test_export_json_and_unknown_format(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    service.export_rows(str(out), "json", ["a", "d"], [[1, Decimal("2.5")]])
    assert json.loads(out.read_text(encoding="utf-8")) == [{"a": 1, "d": "2.5"}]
    with pytest.raises(ValueError, match="Unsupported export format"):
        service.export_rows(out, "xlsx", [], [])


def test_export_failure_leaves_neither_target_nor_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        service.export_rows(tmp_path / "r.csv", "csv", ["a"], [[1]])
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------------------------------------------------- theme
def test_theme_palettes_are_complete_and_consistent() -> None:
    assert PALETTES["light"].keys() == PALETTES["dark"].keys()
    for key in ("stripe", "editor", "fg", "sel", "muted", "warn", "err", "ok", "info", "bg_info", "bg_ok", "bg_warn", "bg_err"):
        assert key in PALETTES["light"]


def test_theme_set_and_unknown_name_falls_back() -> None:
    t = Theme("dark")
    assert t.name == "dark" and t.c["bg"] == PALETTES["dark"]["bg"]
    t.set("nonsense")
    assert t.name == "light" and t.c == PALETTES["light"]
    t.c["bg"] = "#000000"  # the palette is a copy: edits never leak into the shared table
    assert PALETTES["light"]["bg"] != "#000000"


@pytest.mark.parametrize(("platform", "family"), [("win32", "Consolas"), ("darwin", "Menlo"), ("linux", "DejaVu Sans Mono")])
def test_theme_mono_per_platform(monkeypatch: pytest.MonkeyPatch, platform: str, family: str) -> None:
    monkeypatch.setattr("sys.platform", platform)
    assert Theme.mono() == (family, 10)


def test_theme_apply_uses_sv_ttk_when_installed(tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    fake = ModuleType("sv_ttk")
    fake.set_theme = lambda name, root=None: calls.append((name, root))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sv_ttk", fake)
    Theme("dark").apply(tk_root)
    assert calls == [("dark", tk_root)]


def test_theme_apply_falls_back_to_clam(tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sv_ttk", None)  # makes `import sv_ttk` raise ImportError
    Theme("light").apply(tk_root)
    from tkinter import ttk

    assert ttk.Style(tk_root).theme_use() == "clam"


# ---------------------------------------------------------------------------------------------------------- settings
@pytest.fixture
def cfg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(store, "CONFIG", tmp_path / ".db-lens" / "config.json")
    return tmp_path / ".db-lens"


def test_settings_defaults_and_location(cfg_home: Path) -> None:
    s = Settings()
    assert s.data == DEFAULTS and s.data is not DEFAULTS and s.path == cfg_home / "gui.json"
    assert s["theme"] == "light"
    s["theme"] = "dark"
    assert s["theme"] == "dark" and DEFAULTS["theme"] == "light"


def test_settings_roundtrip_private_and_atomic(cfg_home: Path) -> None:
    s = Settings()
    s["limit"] = 250
    s.save()
    assert Settings()["limit"] == 250
    assert [p.name for p in cfg_home.iterdir()] == ["gui.json"]
    assert_mode(s.path, 0o600)
    assert_mode(cfg_home, 0o700)


def test_settings_ignore_unknown_keys_and_corrupt_files(cfg_home: Path) -> None:
    cfg_home.mkdir(parents=True)
    (cfg_home / "gui.json").write_text('{"limit": 5, "evil": 1}', encoding="utf-8")
    s = Settings()
    assert s["limit"] == 5 and "evil" not in s.data
    for bad in ("{not json", "[1, 2]", ""):
        (cfg_home / "gui.json").write_text(bad, encoding="utf-8")
        assert Settings().data == DEFAULTS


def test_settings_history_is_deduplicated_newest_first_and_capped(cfg_home: Path) -> None:
    s = Settings()
    for i in range(MAX_HISTORY + 10):
        s.remember_query(f"SELECT {i}")
    s.remember_query("SELECT 45")
    assert len(s["history"]) == MAX_HISTORY and s["history"][0] == "SELECT 45" and s["history"].count("SELECT 45") == 1


def test_settings_save_failure_never_raises(cfg_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr("os.replace", boom)
    Settings().save()  # must not raise
    monkeypatch.setattr("tempfile.mkstemp", boom)
    Settings().save()


# ----------------------------------------------------------------------------------------------------------- widgets
def test_formatters() -> None:
    assert human_bytes(None) == "—" and human_bytes(512) == "512 B" and human_bytes(2048) == "2 KB" and human_bytes(5 * 1024**4) == "5.0 TB"
    assert fmt_int(None) == "?" and fmt_int(1234567) == "1,234,567" and fmt_int(10, approx=True) == "~10"


def make_grid(frame: tk.Frame, **kw: Any) -> DataGrid:
    g = DataGrid(frame, Theme(), **kw)
    g.pack()
    return g


def test_grid_renders_rows_cleaned_with_null_marker_and_stripes(frame: tk.Frame) -> None:
    seen: list[int] = []
    g = make_grid(frame, max_cell=8, on_rendered=seen.append)
    g.set_data(["id", "txt"], [[1, "a\x1bb"], [2, None], [3, "line1\nline2 is long"]])
    pump(frame, g)
    vals = [g.tree.item(i, "values") for i in g.tree.get_children()]
    assert vals == [("1", "a\\x1bb"), ("2", widgets.NULL_TEXT), ("3", "line1 ⏎ l…")]  # cut to max_cell first, then newlines made visible
    assert seen == [3]
    assert "even" in g.tree.item("0", "tags") and "odd" in g.tree.item("1", "tags")
    assert str(g.tree.column("c0", "anchor")) == "e" and str(g.tree.column("c1", "anchor")) == "w"


def test_grid_newlines_shown_as_symbol_and_formatters_win(frame: tk.Frame) -> None:
    g = make_grid(frame)
    g.set_data(["a", "b"], [["x\ny", 5]], formatters={1: lambda v: f"<{v}>"})
    pump(frame, g)
    assert g.tree.item("0", "values") == ("x ⏎ y", "<5>")


def test_grid_chunked_rendering_is_incremental_and_cancellable(frame: tk.Frame, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DataGrid, "CHUNK", 2)
    done: list[int] = []
    g = make_grid(frame, on_rendered=done.append)
    g.set_data(["n"], [[i] for i in range(7)])
    assert g.rendering and len(g.tree.get_children()) == 2
    for _ in range(20): pump(frame, g)
    assert not peek(g, "rendering") and len(g.tree.get_children()) == 7 and done == [7]
    g.set_data(["n"], [[i] for i in range(7)])  # a new load supersedes a half-finished one
    g.clear()
    pump(frame)
    assert g.tree.get_children() == () and g.columns == [] and done == [7]


def test_grid_sorting_numbers_text_and_nulls(frame: tk.Frame) -> None:
    g = make_grid(frame)
    g.set_data(["v"], [[10], [None], [2], [Decimal("3.5")], [True]])
    pump(frame, g)

    def order() -> list[str]:
        return list(g.tree.get_children())

    g.sort_by(0)
    assert order() == ["2", "3", "0", "4", "1"] and "▲" in g.tree.heading("c0", "text")  # numbers, then text, NULL always last
    g.sort_by(0)
    assert order() == ["4", "0", "3", "2", "1"] and "▼" in g.tree.heading("c0", "text")
    g.set_data(["t"], [["b"], ["A"], ["c"]])
    pump(frame, g)
    g.sort_by(0)
    assert order() == ["1", "0", "2"]


def test_grid_copy_cell_rows_and_all(frame: tk.Frame, tk_root: tk.Tk) -> None:
    g = make_grid(frame)
    g.set_data(["a", "b"], [[1, None], [2, "x"], [3, "y"]])
    pump(frame, g)
    g.copy_cell()  # nothing picked yet: no-op
    g._cell = ("1", "#2")
    g.copy_cell()
    assert tk_root.clipboard_get() == "x"
    g.tree.selection_set(["2", "0"])
    g.copy_selected()
    assert tk_root.clipboard_get() == "1\t\n3\ty"  # selection is copied in display order, NULL as empty
    g.copy_all()
    assert tk_root.clipboard_get().splitlines() == ["a\tb", "1\t", "2\tx", "3\ty"]


def test_grid_keyboard_handlers_and_popup(frame: tk.Frame, monkeypatch: pytest.MonkeyPatch) -> None:
    g = make_grid(frame)
    g.set_data(["a"], [[1], [2]])
    pump(frame, g)
    ev = SimpleNamespace(x=1, y=1, x_root=5, y_root=6)
    shown: list[tuple[int, int]] = []
    monkeypatch.setattr(g.menu, "tk_popup", lambda x, y: shown.append((x, y)))
    monkeypatch.setattr(g.tree, "identify_row", lambda _y: "")
    g._popup(ev)  # type: ignore[arg-type]
    assert shown == []
    monkeypatch.setattr(g.tree, "identify_row", lambda _y: "1")
    monkeypatch.setattr(g.tree, "identify_column", lambda _x: "#1")
    g._popup(ev)  # type: ignore[arg-type]
    assert shown == [(5, 6)] and g.tree.selection() == ("1",) and g._cell == ("1", "#1")
    assert g._on_select_all(ev) == "break"  # type: ignore[arg-type]
    assert set(g.tree.selection()) == {"0", "1"}
    assert g._on_copy(ev) == "break"  # type: ignore[arg-type]


def test_grid_export(frame: tk.Frame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    g = make_grid(frame)
    assert g.export(frame, "csv") is None  # nothing loaded
    g.set_data(["a"], [[2], [1]])
    pump(frame, g)
    g.sort_by(0)
    saveas(monkeypatch, "")
    assert g.export(frame, "csv") is None  # dialog cancelled
    target = tmp_path / "o.csv"
    saveas(monkeypatch, str(target))
    assert g.export(frame, "csv") == str(target)
    assert target.read_text(encoding="utf-8").splitlines() == ["a", "1", "2"]  # as sorted on screen


def test_logpane_levels_details_and_follow(frame: tk.Frame) -> None:
    p = LogPane(frame, Theme("dark"))
    p.pack()
    p.add(0.0, logging.DEBUG, "dbg")
    p.add(0.0, logging.INFO, "info")
    p.add(0.0, logging.WARNING, "warn")
    p.add(0.0, logging.ERROR, "bad")
    text = p.contents()
    assert "dbg" not in text and "▸ info" in text and "⚠ warn" in text and "✖ bad" in text
    p.details.set(True)
    p.render_all()
    assert "· dbg" in p.contents()
    p.follow.set(False)
    p.add(0.0, logging.INFO, "later")
    assert "later" in p.contents()
    p.clear()
    assert p.contents() == "" and len(p.entries) == 0


def test_logpane_ring_buffer_copy_and_save(frame: tk.Frame, tk_root: tk.Tk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LogPane, "MAX", 3)
    p = LogPane(frame, Theme())
    for i in range(5):
        p.add(0.0, logging.INFO, f"m{i}")
    assert [e[2] for e in p.entries] == ["m2", "m3", "m4"]
    p.copy()
    assert "m4" in tk_root.clipboard_get()
    saveas(monkeypatch, "")
    p.save()  # cancelled: writes nothing
    target = tmp_path / "a.log"
    saveas(monkeypatch, str(target))
    p.save()
    assert "m4" in target.read_text(encoding="utf-8") and target.read_text(encoding="utf-8").endswith("\n")


@pytest.mark.parametrize("kind", ["info", "ok", "warn", "err"])
def test_banner_show_hide(frame: tk.Frame, kind: str) -> None:
    b = Banner(frame, Theme())
    b.show(kind, "hello")
    assert b.visible and b.kind == kind and b.label.cget("text") == "hello"
    assert str(b.label.cget("background")) == Theme().c[f"bg_{kind}"]
    b.hide()
    assert not peek(b, "visible")
    b.show(kind, "again")
    ev = SimpleNamespace()
    b._dismiss(ev)  # type: ignore[arg-type]  # the ✕ handler
    assert not peek(b, "visible")
    b._rewrap(ev)  # type: ignore[arg-type]
    assert int(b.label.cget("wraplength")) >= 200


def test_search_entry_notifies_and_clears(frame: tk.Frame) -> None:
    got: list[str] = []
    s = SearchEntry(frame, "find", got.append)
    s.set("abc")
    s.set("")
    s.focus()
    assert got == ["abc", ""] and s.var.get() == ""


def test_status_bar_busy_idle_and_badges(frame: tk.Frame) -> None:
    sb = StatusBar(frame, Theme())
    sb.set_busy("Running", 1.25)
    sb.set_busy("Still running", 2.5)  # second call only updates the labels
    assert sb.busy and sb.op.cget("text") == "Still running" and sb.elapsed.cget("text") == "2.5s"
    sb.set_idle()
    assert not peek(sb, "busy") and sb.op.cget("text") == "Ready"
    sb.set_idle("Done")  # idle while idle: text only
    assert sb.op.cget("text") == "Done"
    sb.set_badges([("RO", "ok"), ("TLS", "info"), ("WRITE", "err")])
    assert len(sb.badges.winfo_children()) == 3
    sb.set_badges([("x", "warn")])
    assert len(sb.badges.winfo_children()) == 1


def test_restyle_methods_follow_theme_changes(frame: tk.Frame) -> None:
    t = Theme("light")
    g, p = make_grid(frame), LogPane(frame, t)
    g.theme = t
    t.set("dark")
    g.restyle()
    p.restyle()
    assert str(p.text.cget("background")) == PALETTES["dark"]["editor"]


def test_widgets_module_has_no_unresolved_names() -> None:
    callables: list[Callable[..., Any]] = [widgets.DataGrid, widgets.LogPane, widgets.Banner, widgets.SearchEntry, widgets.StatusBar]
    assert all(callable(c) for c in callables)


def test_backend_matches_what_the_parent_process_expects(gui_backend: str) -> None:
    """Children started by tests/test_headless.py say which backend (real Tk or the in-process stub) they must have picked."""
    import os

    expected = os.environ.get("DBLENS_EXPECT_GUI_BACKEND")
    assert gui_backend in ("real", "stub") and (expected is None or gui_backend == expected)
