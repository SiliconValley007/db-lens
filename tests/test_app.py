"""`db-lens gui`: the application window (db_lens.gui.app) with real Tk widgets on a hidden root, plus the shared query path."""

from __future__ import annotations

import logging
import time
import tkinter as tk
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from db_lens import cli, db, store
from db_lens.gui import app as gui_app
from db_lens.gui.app import App, QueryResult, describe, execute

from .fakes import FakeConn, FakeEngine, as_real, new_dialect
from .test_gui import pump

USERS = "SELECT id, name FROM users ORDER BY id"


@pytest.fixture
def win(tk_root: tk.Tk, prof: Path) -> Iterator[App]:
    top = tk.Toplevel(tk_root)
    app = App(top, "shop")
    yield app
    if top.winfo_exists():
        app.close()


def wait(app: App) -> None:
    deadline = time.monotonic() + 10
    while app.busy and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert not app.busy, "the statement did not finish"
    pump(app.root, app.grid_view)


def type_sql(app: App, sql: str) -> None:
    app.editor.delete("1.0", "end")
    app.editor.insert("1.0", sql)


@contextmanager
def fake_session(rec: dict[str, Any], script: Any = ()) -> Iterator[Any]:
    conn = FakeConn(new_dialect(postgresql), [("SHOW transaction_read_only", [("on",)]), *script])
    yield as_real(FakeEngine(conn)), rec


# ------------------------------------------------------------------------------------------------------------- execute()
def test_execute_returns_rows_columns_and_timing(prof: Path) -> None:
    res = execute("shop", USERS, limit=100)
    assert res.columns == ["id", "name"] and len(res.rows) == 6 and not res.truncated and res.warnings == [] and res.millis > 0


def test_execute_truncates_and_trims_the_statement(prof: Path) -> None:
    res = execute("shop", f"  {USERS} ;  ", limit=2)
    assert len(res.rows) == 2 and res.truncated


@pytest.mark.parametrize("sql", ["", "   ;  "])
def test_execute_refuses_empty_input(prof: Path, sql: str) -> None:
    with pytest.raises(ValueError, match="Enter a SQL statement"):
        execute("shop", sql)


@pytest.mark.parametrize("sql", ["DELETE FROM users", "DROP TABLE users", "INSERT INTO users VALUES (9, 'x')"])
def test_execute_refuses_writes_before_touching_the_database(prof: Path, sql: str) -> None:
    with pytest.raises(Exception, match="read-only|only|not allowed|Refus"):
        execute("shop", sql)
    assert execute("shop", "SELECT count(*) AS n FROM users").rows == [[6]]


def test_execute_strict_demands_a_verifiably_read_only_account(prof: Path) -> None:
    with pytest.raises(ValueError, match="Strict mode: not verifiably read-only"):
        execute("shop", USERS, strict=True)


def test_execute_honours_the_profile_schema(home: Path, dbfile: Path, runner: Any) -> None:
    runner("connect", f"sqlite:///{dbfile}", "-n", "s", "-S", "main", has="Connected")
    assert len(execute("s", USERS).rows) == 6


def test_execute_unknown_profile(home: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        execute("nope", "SELECT 1")


def test_execute_warns_about_writable_accounts_and_user_routines(monkeypatch: pytest.MonkeyPatch) -> None:
    risk = {"kind": "custom-routine", "names": ["myfn"], "message": "m", "blocking": True}
    monkeypatch.setattr(cli, "session", lambda profile: fake_session({"writable": True, "risks": [risk]}, [("myfn", [(1,)])]))
    res = execute("p", "SELECT myfn(1)")
    assert len(res.warnings) == 2 and "modify data" in res.warnings[0] and "myfn" in res.warnings[1]


def test_execute_strict_refuses_user_routine_calls_even_on_a_read_only_account(monkeypatch: pytest.MonkeyPatch) -> None:
    risk = {"kind": "custom-routine", "names": ["myfn"], "message": "m", "blocking": False}
    monkeypatch.setattr(cli, "session", lambda profile: fake_session({"risks": [risk]}))
    monkeypatch.setattr(db, "probe_account", lambda e: (False, "", []))
    risk["blocking"] = True
    with pytest.raises(ValueError, match="statement calls myfn"):
        execute("p", "SELECT myfn(1)", strict=True)


def test_execute_carries_session_notices_as_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def noisy(profile: Any) -> Iterator[Any]:
        with fake_session({}) as (eng, rec):
            real_connect = eng.connect

            def connect() -> Any:
                c = real_connect()
                c.info["warnings"] = [db.Notice("X", "heads up")]
                return c

            eng.connect = connect
            yield eng, rec

    monkeypatch.setattr(cli, "session", noisy)
    assert execute("p", "SELECT 1").warnings == ["heads up [X]"]


def test_describe_is_redacted_cleaned_and_never_empty() -> None:
    store.remember("s3cretvalue")
    assert "s3cretvalue" not in describe(RuntimeError("postgresql://u:s3cretvalue@h/d broke \x1b[31m"))
    assert "\x1b" not in describe(RuntimeError("a\x1bb"))
    assert describe(RuntimeError("")) == "RuntimeError"
    inner = RuntimeError("driver text")
    wrapped = RuntimeError("wrapper")
    wrapped.orig = inner  # type: ignore[attr-defined]
    assert describe(wrapped) == "driver text"


def test_pane_handler_redacts_and_drops_library_notices() -> None:
    store.remember("hunter2hunter2")
    seen: list[tuple[float, int, str]] = []
    h = gui_app._PaneHandler(lambda *a: seen.append(a))
    mk = logging.LogRecord
    h.emit(mk("db_lens", logging.INFO, "f", 1, "pw=%s", ("hunter2hunter2",), None))
    notice = mk("db_lens", logging.WARNING, "f", 1, "noted", (), None)
    notice.code = "X"
    h.emit(notice)
    assert len(seen) == 1 and "hunter2" not in seen[0][2] and seen[0][1] == logging.INFO


# ---------------------------------------------------------------------------------------------------------- the window
def test_window_starts_on_the_requested_profile_with_badges(win: App) -> None:
    assert win.profile.get() == "shop" and win.root.title() == "db-lens"
    labels = [str(w.cget("text")).strip() for w in win.status.badges.winfo_children()]
    assert labels[:2] == ["sqlite", "read-only session"] and "account can write" in labels
    assert win.status.op.cget("text") == "Profile shop" and not win.banner.visible


def test_window_badges_show_tls_and_ssh(home: Path, tk_root: tk.Tk) -> None:
    store.add("p", "postgresql://u@h/d", tls="require", ssh={"host": "bastion"}, writable=False)
    top = tk.Toplevel(tk_root)
    app = App(top)
    assert app.profile.get() == "p"
    labels = [str(w.cget("text")).strip() for w in app.status.badges.winfo_children()]
    assert labels == ["postgresql", "read-only session", "TLS require", "ssh tunnel"]
    app.close()


def test_window_without_profiles_explains_what_to_do(home: Path, tk_root: tk.Tk) -> None:
    top = tk.Toplevel(tk_root)
    app = App(top, "missing")
    assert app.banner.visible and "db-lens connect" in app.banner.label.cget("text") and app.profile.get() == ""
    app.close()


def test_window_unknown_profile_falls_back_to_the_default(win: App, tk_root: tk.Tk) -> None:
    top = tk.Toplevel(tk_root)
    other = App(top, "does-not-exist")
    assert other.profile.get() == "shop"
    other.close()


def test_run_shows_rows_status_history_and_activity(win: App) -> None:
    type_sql(win, USERS)
    win.run()
    assert win.busy and win.run_btn.instate(["disabled"])
    win.run()  # a second Run while busy is ignored
    wait(win)
    assert len(win.grid_view.tree.get_children()) == 6 and win.result is not None and win.result.columns == ["id", "name"]
    assert "6 row(s)" in win.status.op.cget("text") and not win.banner.visible
    assert win.settings["history"][0] == USERS and win.history.cget("values")[0] == USERS
    assert "running a statement on profile shop" in win.log.contents()


def test_run_truncated_result_explains_the_limit(win: App) -> None:
    win.limit.set(2)
    type_sql(win, USERS)
    win.run()
    wait(win)
    assert len(win.grid_view.tree.get_children()) == 2 and win.banner.visible and "raise Limit" in win.banner.label.cget("text")
    assert "truncated" in win.status.op.cget("text")


def test_run_failure_is_a_redacted_banner_and_the_window_stays_usable(win: App) -> None:
    type_sql(win, "DELETE FROM users")
    win.run()
    wait(win)
    assert win.banner.visible and win.banner.kind == "err" and win.status.op.cget("text") == "Failed"
    assert not win.run_btn.instate(["disabled"]) and "✖" in win.log.contents()
    type_sql(win, USERS)
    win.run()
    wait(win)
    assert len(win.grid_view.tree.get_children()) == 6 and not win.banner.visible


def test_run_with_nothing_typed_asks_for_sql(win: App) -> None:
    type_sql(win, "  ")
    win.run()
    assert not win.busy and win.banner.kind == "warn" and "Enter a SQL" in win.banner.label.cget("text")


def test_run_shows_warnings_from_the_query_path(win: App, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui_app, "execute", lambda *a: QueryResult(["c"], [[1]], False, ["careful", "very"], 1.0))
    type_sql(win, "SELECT 1")
    win.run()
    wait(win)
    assert win.banner.kind == "warn" and win.banner.label.cget("text") == "careful very"


def test_keyboard_shortcut_runs_and_swallows_the_key(win: App) -> None:
    type_sql(win, USERS)
    assert win._on_run_key(None) == "break"  # type: ignore[arg-type]
    wait(win)
    assert win.result is not None


def test_history_and_profile_handlers(win: App) -> None:
    win.history.set("SELECT 42")
    win._on_history(None)  # type: ignore[arg-type]
    assert str(win.editor.get("1.0", "end")).strip() == "SELECT 42"
    win.banner.show("err", "stale")
    win._on_profile(None)  # type: ignore[arg-type]
    assert not win.banner.visible


def test_export_writes_the_rows_and_reports_the_path(win: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    type_sql(win, USERS)
    win.run()
    wait(win)
    target = tmp_path / "out.csv"
    monkeypatch.setattr("tkinter.filedialog.asksaveasfilename", lambda **k: str(target))
    win.export_csv()
    assert (
        target.read_text(encoding="utf-8").splitlines()[0] == "id,name" and win.banner.kind == "ok" and str(target) in win.banner.label.cget("text")
    )
    target_json = tmp_path / "out.json"
    monkeypatch.setattr("tkinter.filedialog.asksaveasfilename", lambda **k: str(target_json))
    win.export_json()
    assert target_json.exists()
    monkeypatch.setattr("tkinter.filedialog.asksaveasfilename", lambda **k: "")
    win.banner.hide()
    win.export("csv")  # cancelled: nothing reported
    assert not win.banner.visible


def test_export_failure_is_shown_not_raised(win: App, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(win.grid_view, "export", boom)
    win.export("csv")
    assert win.banner.kind == "err" and "disk full" in win.banner.label.cget("text")


def test_theme_and_log_toggles_persist_in_settings(win: App) -> None:
    before = win.theme.name
    win.toggle_theme()
    assert win.theme.name != before and win.settings["theme"] == win.theme.name
    assert str(win.editor.cget("background")) == win.theme.c["editor"]
    win.toggle_theme()
    assert win.theme.name == before
    shown = win.log_visible
    win.toggle_log()
    assert win.log_visible is not shown and win.settings["show_log"] is win.log_visible
    win.toggle_log()
    assert win.log_visible is shown


def test_close_saves_preferences_and_restores_logging(tk_root: tk.Tk, prof: Path) -> None:
    logger = logging.getLogger("db_lens")
    level, handlers = logger.level, list(logger.handlers)
    top = tk.Toplevel(tk_root)
    app = App(top, "shop")
    assert logger.level == logging.DEBUG and app.handler in logger.handlers
    app.limit.set(7)
    app.strict.set(True)
    app.close()
    assert logger.level == level and logger.handlers == handlers and not top.winfo_exists()
    saved = Path(store.CONFIG).parent / "gui.json"
    assert '"limit": 7' in saved.read_text(encoding="utf-8") and '"strict": true' in saved.read_text(encoding="utf-8")


def test_saved_preferences_are_applied_on_the_next_start(tk_root: tk.Tk, prof: Path) -> None:
    top = tk.Toplevel(tk_root)
    first = App(top, "shop")
    first.limit.set(33)
    first.toggle_theme()
    first.close()
    top2 = tk.Toplevel(tk_root)
    second = App(top2, "shop")
    assert second.limit.get() == 33 and second.theme.name == "dark"
    second.close()


def test_main_opens_runs_the_loop_and_returns(tk_root: tk.Tk, prof: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    top = tk.Toplevel(tk_root)
    top.after(30, top.quit)
    gui_app.main("shop", root=top)
    assert top.winfo_exists()
    top.destroy()
    top2 = tk.Toplevel(tk_root)
    top2.after(30, top2.quit)
    monkeypatch.setattr("tkinter.Tk", lambda: top2)
    gui_app.main()
    top2.destroy()
