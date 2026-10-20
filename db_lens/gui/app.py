"""The db-lens desktop window (``db-lens gui``): pick a saved profile, run one read-only statement, browse and export the rows.

Every statement goes through exactly the same checks as ``db-lens query`` (SQL guard, verified read-only session, timeout, row limit,
``--strict``). Work happens on a worker thread; the Tk thread only ever reads the event queue, so the window never freezes and no widget
is touched from another thread."""

from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field
from tkinter import ttk
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import make_url, text
from sqlglot import exp

from .. import cli, db, sqlguard, store
from .settings import Settings
from .theme import Theme
from .widgets import Banner, DataGrid, LogPane, StatusBar, fmt_int

POLL_MS = 40


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    warnings: list[str] = field(default_factory=list)
    millis: float = 0.0


def execute(profile: str | None, sql: str, limit: int = 100, timeout: float = 30.0, strict: bool = False) -> QueryResult:
    """Run one statement the way ``db-lens query`` does. Raises on every refusal or failure (the caller shows it, redacted)."""
    stmt = sql.strip().rstrip(";").strip()
    if not stmt:
        raise ValueError("Enter a SQL statement.")
    t0 = time.perf_counter()
    warnings: list[str] = []
    with cli.session(profile) as (eng, rec):
        root = sqlguard.validate(stmt, eng.dialect.name)
        sch = rec.get("schema")
        if sch:
            sch = db.resolve_schema(sa_inspect(eng), sch)
        stored = cli.blocking(rec.get("risks", []))
        routines = {n for r in stored if r["kind"] == "custom-routine" for n in r.get("names", [])}
        hits, udfs = sqlguard.classify_calls(root, routines)
        if strict:
            ok, why, risks = db.probe_account(eng)
            blocked = [r.message for r in risks if r.blocking]
            if ok is not False or blocked or hits or udfs:
                detail = why or "; ".join(blocked) or f"statement calls {', '.join(hits + udfs)}"
                raise ValueError(f"Strict mode: not verifiably read-only ({detail or 'unknown'}). {sqlguard.NOTICE}")
        elif eng.dialect.name != "sqlite" and (rec.get("writable") or stored):
            warnings.append("This account can modify data or run user-created routines; the SQL check cannot see inside them.")
        if hits or udfs:
            warnings.append(f"Calls {', '.join((hits + udfs)[:5])}: the SQL check cannot see what it does; it may write.")
        stream = isinstance(root, exp.Query) and eng.dialect.name in ("postgresql", "mysql", "mariadb")
        with db.connect_ro(eng, sch, timeout) as c:
            warnings += [str(w) for w in c.info.get("warnings", [])]
            cols, rows, trunc = db.fetch_limited(c, text(stmt), limit, stream)
    return QueryResult(list(cols), [list(r) for r in rows], trunc, warnings, (time.perf_counter() - t0) * 1000)


def describe(e: BaseException) -> str:
    """One safe line for a failure: driver text without secrets or control bytes."""
    return cli.clean(store.redact(str(getattr(e, "orig", None) or e).strip()))[:600] or type(e).__name__


class _PaneHandler(logging.Handler):
    """Forward db_lens log records (redacted) to a sink; library notices carry a ``code`` and are shown as banners instead."""

    def __init__(self, sink: Callable[[float, int, str], None]) -> None:
        super().__init__(logging.DEBUG)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        if not hasattr(record, "code"):
            self.sink(record.created, record.levelno, store.redact(record.getMessage()))


class App:
    def __init__(self, root: tk.Tk | tk.Toplevel, profile: str | None = None, settings: Settings | None = None) -> None:
        self.root = root
        self.settings = settings or Settings()
        self.theme = Theme(str(self.settings["theme"]))
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy, self.log_visible, self.started = False, False, 0.0
        self.result: QueryResult | None = None
        self.profile = tk.StringVar(master=root)
        self.limit = tk.IntVar(master=root, value=int(self.settings["limit"]))
        self.timeout = tk.IntVar(master=root, value=int(self.settings["timeout"]))
        self.strict = tk.BooleanVar(master=root, value=bool(self.settings["strict"]))
        self._build()
        names = list(self.profile_box.cget("values"))
        default = store.load().get("default") or (names[0] if names else "")
        self.profile.set(profile if profile is not None and profile in names else str(default))
        self._select_profile()
        self.logger, self.old_level = logging.getLogger("db_lens"), logging.getLogger("db_lens").level
        self.handler = _PaneHandler(lambda *a: self.events.put(("log", a)))
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        if self.settings["show_log"]:
            self.toggle_log()
        root.protocol("WM_DELETE_WINDOW", self.close)

    # -- layout --------------------------------------------------------------------------------------------------------------
    def _build(self) -> None:
        r = self.root
        r.title("db-lens")
        r.geometry(str(self.settings["geometry"]))
        r.columnconfigure(0, weight=1)
        r.rowconfigure(3, weight=1)
        self.theme.apply(r)
        bar = ttk.Frame(r)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        ttk.Label(bar, text="Profile").pack(side="left")
        self.profile_box = ttk.Combobox(bar, textvariable=self.profile, values=list(store.load()["profiles"]), state="readonly", width=22)
        self.profile_box.pack(side="left", padx=(4, 10))
        self.profile_box.bind("<<ComboboxSelected>>", self._on_profile)
        ttk.Label(bar, text="Limit").pack(side="left")
        ttk.Spinbox(bar, from_=1, to=100000, textvariable=self.limit, width=7).pack(side="left", padx=(4, 8))
        ttk.Label(bar, text="Timeout s").pack(side="left")
        ttk.Spinbox(bar, from_=0, to=3600, textvariable=self.timeout, width=5).pack(side="left", padx=(4, 8))
        ttk.Checkbutton(bar, text="Strict", variable=self.strict).pack(side="left", padx=4)
        self.run_btn = ttk.Button(bar, text="Run (Ctrl+Enter)", command=self.run)
        self.run_btn.pack(side="left", padx=8)
        for label, cmd in (
            ("Theme", self.toggle_theme),
            ("Log", self.toggle_log),
            ("CSV…", lambda: self.export("csv")),
            ("JSON…", lambda: self.export("json")),
        ):
            ttk.Button(bar, text=label, command=cmd).pack(side="right", padx=2)
        self.history = ttk.Combobox(r, values=list(self.settings["history"]), state="readonly")
        self.history.grid(row=1, column=0, sticky="ew", padx=8, pady=2)
        self.history.bind("<<ComboboxSelected>>", self._on_history)
        self.banner = Banner(r, self.theme)
        self.banner.grid(row=2, column=0, sticky="ew", padx=8, pady=2)
        self.banner.hide()
        self.paned = ttk.PanedWindow(r, orient="vertical")
        self.paned.grid(row=3, column=0, sticky="nsew", padx=8, pady=2)
        self.editor = tk.Text(self.paned, height=6, wrap="word", undo=True, font=Theme.mono())
        self.editor.bind("<Control-Return>", self._on_run_key)
        self.editor.bind("<F5>", self._on_run_key)
        self.grid_view = DataGrid(self.paned, self.theme, on_rendered=self._rendered)
        self.paned.add(self.editor, weight=1)
        self.paned.add(self.grid_view, weight=4)
        self.log = LogPane(self.paned, self.theme)
        self.status = StatusBar(r, self.theme)
        self.status.grid(row=4, column=0, sticky="ew")
        self._restyle()

    def _restyle(self) -> None:
        c = self.theme.c
        self.editor.configure(background=c["editor"], foreground=c["fg"], insertbackground=c["fg"], selectbackground=c["sel"])
        self.grid_view.restyle()
        self.log.restyle()
        self.banner.restyle()

    # -- actions -------------------------------------------------------------------------------------------------------------
    def _on_profile(self, _event: tk.Event[tk.Misc]) -> None:
        self._select_profile()

    def _on_history(self, _event: tk.Event[tk.Misc]) -> None:
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", self.history.get())

    def _on_run_key(self, _event: tk.Event[tk.Misc]) -> str:
        self.run()
        return "break"

    def _select_profile(self) -> None:
        name, rec = self.profile.get(), store.load()["profiles"].get(self.profile.get())
        if rec is None:
            self.banner.show("info", "No saved profiles. Create one first: db-lens connect <uri>")
            self.status.set_badges([])
            return
        self.banner.hide()
        badges = [(make_url(rec["uri"]).get_backend_name(), "info"), ("read-only session", "ok")]
        if rec.get("tls"):
            badges.append((f"TLS {rec['tls']}", "ok"))
        if rec.get("ssh"):
            badges.append(("ssh tunnel", "info"))
        if rec.get("writable"):
            badges.append(("account can write", "warn"))
        self.status.set_badges(badges)
        self.status.set_idle(f"Profile {name}")

    def run(self) -> None:
        if self.busy:
            return
        sql = str(self.editor.get("1.0", "end")).strip()
        if not sql:
            self.banner.show("warn", "Enter a SQL statement first.")
            return
        self.settings.remember_query(sql)
        self.history.configure(values=list(self.settings["history"]))
        profile, limit, timeout, strict = self.profile.get() or None, self.limit.get(), float(self.timeout.get()), self.strict.get()
        self.busy, self.started = True, time.perf_counter()
        self.run_btn.state(["disabled"])
        self.banner.hide()
        self.status.set_busy("Running…", 0.0)
        self.logger.info("running a statement on profile %s", profile)

        def work() -> None:
            try:
                self.events.put(("done", execute(profile, sql, limit, timeout, strict)))
            except Exception as e:  # noqa: BLE001 - everything is reported to the window, nothing escapes the thread
                self.events.put(("error", e))

        threading.Thread(target=work, daemon=True, name="db-lens-query").start()
        self.root.after(POLL_MS, self._poll)

    def _poll(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log.add(*payload)
            elif kind == "done":
                self._done(payload)
            else:
                self._failed(payload)
        if self.busy:
            self.status.set_busy("Running…", time.perf_counter() - self.started)
            self.root.after(POLL_MS, self._poll)

    def _done(self, res: QueryResult) -> None:
        self.busy, self.result = False, res
        self.run_btn.state(["!disabled"])
        self.grid_view.set_data(res.columns, res.rows)
        note = f"{fmt_int(len(res.rows))} row(s) · {res.millis:.1f} ms" + (" · truncated" if res.truncated else "")
        self.status.set_idle(note)
        if res.warnings:
            self.banner.show("warn", " ".join(res.warnings))
        elif res.truncated:
            self.banner.show("info", f"Showing the first {len(res.rows):,} rows; raise Limit to see more.")

    def _failed(self, e: BaseException) -> None:
        self.busy = False
        self.run_btn.state(["!disabled"])
        message = describe(e)
        self.logger.error("%s: %s", type(e).__name__, message)
        self.banner.show("err", message)
        self.status.set_idle("Failed")

    def _rendered(self, _n: int) -> None:
        self.status.set_idle(self.status.op.cget("text"))

    def export(self, fmt: str) -> None:
        try:
            path = self.grid_view.export(self.root, fmt)
        except OSError as e:
            self.banner.show("err", describe(e))
            return
        if path:
            self.banner.show("ok", f"Saved {path}")

    def export_csv(self) -> None:
        self.export("csv")

    def export_json(self) -> None:
        self.export("json")

    def toggle_theme(self) -> None:
        self.theme.set("dark" if self.theme.name == "light" else "light")
        self.settings["theme"] = self.theme.name
        self.theme.apply(self.root)
        self._restyle()

    def toggle_log(self) -> None:
        if self.log_visible:
            self.paned.forget(self.log)
        else:
            self.paned.add(self.log, weight=1)
        self.log_visible = not self.log_visible
        self.settings["show_log"] = self.log_visible

    def close(self) -> None:
        self.settings["geometry"] = self.root.geometry()
        self.settings["limit"], self.settings["timeout"], self.settings["strict"] = self.limit.get(), self.timeout.get(), self.strict.get()
        self.settings.save()
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)
        self.root.destroy()


def main(profile: str | None = None, root: tk.Tk | tk.Toplevel | None = None) -> None:
    """Open the window and block until it is closed."""
    win = root or tk.Tk()
    App(win, profile)
    win.mainloop()
