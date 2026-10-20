from __future__ import annotations

import gc
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner, Result

from db_lens import store, tunnel

if TYPE_CHECKING:
    import tkinter as tk


class FakeKeyring:
    """In-memory keyring; `lossy=True` mimics a null backend that accepts and silently discards secrets."""

    def __init__(self, lossy: Any = False) -> None:
        self.d: dict[tuple[str, str], str] = {}
        self.lossy = lossy

    def set_password(self, s: Any, n: Any, p: Any) -> Any:
        if not self.lossy:
            self.d[(s, n)] = p

    def get_password(self, s: Any, n: Any) -> Any:
        return self.d.get((s, n))

    def delete_password(self, s: Any, n: Any) -> Any:
        self.d.pop((s, n), None)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(store, "CONFIG", tmp_path / ".db-lens" / "config.json")
    monkeypatch.setattr(store, "_keyring", lambda: None)
    monkeypatch.setattr(store, "_SECRETS", set())
    return tmp_path


@pytest.fixture
def keyring(home: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    kr = FakeKeyring()
    monkeypatch.setattr(store, "_keyring", lambda: kr)
    return kr


@pytest.fixture
def dbfile(tmp_path: Path) -> Any:
    p = tmp_path / "shop.db"
    c = sqlite3.connect(p)
    c.executescript("""
    CREATE TABLE parent(a INT, b INT, PRIMARY KEY(a,b));
    CREATE TABLE "order items"(id INTEGER PRIMARY KEY, "unit price" REAL NOT NULL CHECK("unit price" >= 0),
      pa INT, pb INT, sku TEXT UNIQUE, note TEXT, FOREIGN KEY(pa,pb) REFERENCES parent(a,b));
    CREATE INDEX ix_note ON "order items"(note);
    CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);
    CREATE VIEW v_users AS SELECT id, name FROM users;
    CREATE TABLE "we""ird [red]name"("co""l" TEXT, "[bold]c2" TEXT);
    INSERT INTO parent VALUES (1,2); INSERT INTO "order items" VALUES (1,9.5,1,2,'s1','[red]x[/red]');
    INSERT INTO users VALUES (1,'a'),(2,'b'),(3,NULL),(4,char(27)||'[31mEVIL'||char(7)),(5,'NULL'),(6,'');
    INSERT INTO "we""ird [red]name" VALUES ('v1','v2');""")
    c.commit()
    c.close()
    return p


class Runner:
    """Invoke the CLI in-process; ``last`` keeps the full Result (stdout + stderr) for assertions about error panels."""

    last: Result

    def __init__(self) -> None:
        self._r = CliRunner()

    def __call__(self, *args: str, code: int = 0, has: str | Iterable[str] = (), hasnt: str | Iterable[str] = (), inp: str | None = None) -> str:
        from db_lens import cli

        x = self._r.invoke(cli.app, list(args), input=inp)
        assert x.exit_code == code, (args, x.output)
        assert "Traceback" not in x.output, x.output  # failures must be panels, never stack traces
        for h in [has] if isinstance(has, str) else has:
            assert h in re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", x.output), (args, h, x.output)
        for h in [hasnt] if isinstance(hasnt, str) else hasnt:
            assert h not in x.output, (args, h, x.output)
        self.last = x
        return x.stdout


@pytest.fixture
def runner() -> Runner:
    return Runner()


@pytest.fixture
def prof(home: Path, dbfile: Path, runner: Any) -> Any:
    runner("connect", f"sqlite:///{dbfile}", "-n", "shop", has="Connected")
    return dbfile


# ---- GUI backend: real Tk (own display, or a private Xvfb) when possible, otherwise the deterministic in-process stub ----------------------
_XVFB: subprocess.Popen[str] | None = None


def _stop_xvfb() -> None:
    global _XVFB
    if _XVFB is not None:
        _XVFB.terminate()
        _XVFB.wait(timeout=5)
        os.environ.pop("DISPLAY", None)
        _XVFB = None


def _choose_gui_backend() -> str:
    """'real' or 'stub'. DBLENS_TK_BACKEND=auto (default) | real | stub (DBLENS_TK_STUB=1 is an alias for stub). Auto uses the stub when tkinter is missing
    or when no display can be had (Linux without $DISPLAY and without Xvfb, or ``tkinter.Tk()`` raising TclError); Windows and macOS use their native Tk
    and never need $DISPLAY or Xvfb. 'real' never falls back. Nothing is ever skipped."""
    global _XVFB
    mode = os.environ.get("DBLENS_TK_BACKEND", "").lower() or ("stub" if os.environ.get("DBLENS_TK_STUB") == "1" else "auto")
    if mode == "stub":
        return "stub"
    try:
        import tkinter
    except ImportError:
        if mode == "real":
            raise
        return "stub"
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and (exe := shutil.which("Xvfb")):
        _XVFB = subprocess.Popen(
            [exe, "-displayfd", "1", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        assert _XVFB.stdout is not None
        os.environ["DISPLAY"] = f":{_XVFB.stdout.readline().strip()}"
        _XVFB.stdout.close()  # only the display number was needed; an open pipe would surface as a ResourceWarning later
    try:
        probe = tkinter.Tk()
        probe.destroy()
    except tkinter.TclError:
        _stop_xvfb()
        if mode == "real":  # explicitly requested: fail loudly instead of quietly testing the stub
            raise
        return "stub"
    return "real"


GUI_BACKEND = _choose_gui_backend()
if GUI_BACKEND == "stub":
    from . import tkstub

    tkstub.install()  # before any test module (or db_lens.gui) imports tkinter


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _stop_xvfb()


@pytest.fixture(autouse=True)
def _collect_tk_garbage(request: pytest.FixtureRequest) -> Iterator[None]:
    """Real Tk objects (variables, widgets) in reference cycles must be finalised on the main thread: a collection triggered on the worker
    thread of a later test would raise 'main thread is not in main loop' from Variable.__del__. Collect right after each GUI test instead."""
    yield
    if GUI_BACKEND == "real" and request.module.__name__.rsplit(".", 1)[-1] in {"test_gui", "test_app"}:
        gc.collect()


@pytest.fixture(scope="session")
def gui_backend() -> str:
    return GUI_BACKEND


@pytest.fixture(scope="session")
def tk_root() -> Iterator[tk.Tk]:
    """A hidden Tk root: real (own display or private Xvfb) or the in-process stub; the GUI tests run on either and never skip."""
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    yield root
    root.destroy()


# ---- operating-system simulation -----------------------------------------------------------------------------------------------------------
class _PosixFcntl:
    """Stands in for the fcntl module where it does not exist (a real Windows host simulating POSIX)."""

    LOCK_EX, LOCK_UN = 2, 8

    def flock(self, f: Any, op: int) -> None:
        return None


def _use_os(mp: pytest.MonkeyPatch, windows: bool) -> None:
    mp.setattr(store, "_is_windows", lambda: windows)
    mp.setattr(tunnel, "_is_windows", lambda: windows)
    if windows:
        mp.setattr(store, "fcntl", None)
    elif store.fcntl is None:
        mp.setattr(store, "fcntl", _PosixFcntl())


@pytest.fixture(autouse=True)
def _simulated_os(monkeypatch: pytest.MonkeyPatch) -> None:
    """DBLENS_SIMULATE_OS=windows|posix runs the WHOLE suite with that platform's helper defaults, on any host (CI gate for the other OS)."""
    sim = os.environ.get("DBLENS_SIMULATE_OS", "").lower()
    if sim in ("windows", "posix"):
        _use_os(monkeypatch, sim == "windows")


@pytest.fixture
def as_posix(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """This test sees POSIX helpers (``_is_windows() -> False``, an fcntl present) whatever the host is."""
    _use_os(monkeypatch, False)
    return monkeypatch


@pytest.fixture
def as_windows(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """This test sees Windows helpers (``_is_windows() -> True``, no fcntl) whatever the host is."""
    _use_os(monkeypatch, True)
    return monkeypatch
