"""`pytest` must pass out of the box on a bare machine: with Xvfb, with only an in-process stub, and with neither a display nor Xvfb."""

from __future__ import annotations

import importlib.machinery
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from . import conftest

ROOT = Path(__file__).resolve().parent.parent


def run_child(env_extra: dict[str, str], drop: tuple[str, ...] = (), path: str | None = None, expect_ok: bool = True) -> str:
    """Run the GUI suites in a fresh pytest. DISPLAY / backend variables are dropped first (Windows and macOS have no $DISPLAY to drop)."""
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "DBLENS_TK_STUB", "DBLENS_TK_BACKEND", *drop)} | env_extra
    if path is not None:
        env["PATH"] = path
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_gui.py", "tests/test_app.py", "-q", "--no-cov", "-p", "no:cacheprovider", "-rs"],
        capture_output=True, text=True, env=env, timeout=300, check=False, cwd=ROOT,
    )  # fmt: skip
    out = p.stdout + p.stderr
    if not expect_ok:
        assert p.returncode != 0, out
        return out
    assert p.returncode == 0 and " passed" in out, out
    assert "skipped" not in out and "failed" not in out and "error" not in out.lower().replace("0 errors", ""), out
    return out


def linux() -> bool:
    return sys.platform.startswith("linux")


def real_tk_possible() -> bool:
    """A real Tk can be had without $DISPLAY: always on Windows (GDI) and macOS (Aqua); on Linux only through a private Xvfb."""
    return importlib.machinery.PathFinder.find_spec("tkinter") is not None and (not linux() or shutil.which("Xvfb") is not None)


def test_auto_backend_is_real_where_possible_and_the_stub_elsewhere() -> None:
    """Auto mode, DISPLAY unset: real Tk on Windows/macOS (no X11 involved) and on Linux with Xvfb; the stub on a bare Linux container."""
    run_child({"DBLENS_EXPECT_GUI_BACKEND": "real" if real_tk_possible() else "stub"})


def test_real_backend_runs_the_gui_suites_where_possible_and_fails_loudly_where_not() -> None:
    if real_tk_possible():
        run_child({"DBLENS_TK_BACKEND": "real", "DBLENS_EXPECT_GUI_BACKEND": "real"})
    else:  # bare Linux: forcing real must not quietly test the stub
        out = run_child({"DBLENS_TK_BACKEND": "real"}, expect_ok=False)
        assert "TclError" in out or "ImportError" in out or "ModuleNotFoundError" in out, out


def test_stub_backend_on_every_platform() -> None:
    run_child({"DBLENS_TK_BACKEND": "stub", "DBLENS_EXPECT_GUI_BACKEND": "stub"})


def test_legacy_stub_switch_is_still_honoured() -> None:
    run_child({"DBLENS_TK_STUB": "1", "DBLENS_EXPECT_GUI_BACKEND": "stub"})


def test_stub_when_a_linux_container_has_neither_a_display_nor_xvfb(tmp_path: Path) -> None:
    """The minimal-container case through the real auto path (empty PATH). Other platforms always have a native Tk, so there is nothing to fall back from."""
    if linux():
        run_child({"DBLENS_EXPECT_GUI_BACKEND": "stub"}, path=str(tmp_path))
    else:
        run_child({"DBLENS_EXPECT_GUI_BACKEND": "real" if real_tk_possible() else "stub"})


# ---- the selection logic itself, in-process, with fake tkinter modules ---------------------------------------------------
class DeadTk(types.ModuleType):
    TclError = type("TclError", (Exception,), {})

    def Tk(self) -> Any:  # noqa: N802 - mimics tkinter.Tk
        raise self.TclError("no display name and no $DISPLAY environment variable")


class LiveTk(types.ModuleType):
    TclError = type("TclError", (Exception,), {})

    def Tk(self) -> Any:  # noqa: N802
        return types.SimpleNamespace(destroy=lambda: None)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DISPLAY", "placeholder")  # records "was absent" so teardown removes whatever a test sets directly
    monkeypatch.delenv("DISPLAY")
    monkeypatch.delenv("DBLENS_TK_STUB", raising=False)
    monkeypatch.delenv("DBLENS_TK_BACKEND", raising=False)
    monkeypatch.setattr(conftest, "_XVFB", None)
    return monkeypatch


def test_choose_backend_stub_when_forced(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DBLENS_TK_STUB", "1")
    assert conftest._choose_gui_backend() == "stub"


def test_choose_backend_stub_when_tkinter_is_missing(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setitem(sys.modules, "tkinter", None)  # makes `import tkinter` raise ImportError
    assert conftest._choose_gui_backend() == "stub"


def test_choose_backend_stub_when_no_display_and_no_xvfb(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setitem(sys.modules, "tkinter", DeadTk("tkinter"))
    clean_env.setattr(shutil, "which", lambda name, *a, **k: None)
    assert conftest._choose_gui_backend() == "stub"


def test_choose_backend_real_when_tk_opens(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DISPLAY", ":42")
    clean_env.setitem(sys.modules, "tkinter", LiveTk("tkinter"))
    assert conftest._choose_gui_backend() == "real"


def test_choose_backend_starts_xvfb_and_stops_it_again_when_tk_still_fails(clean_env: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class FakeXvfb:
        stdout = types.SimpleNamespace(readline=lambda: "99\n", close=lambda: events.append("pipe closed"))

        def __init__(self, cmd: list[str], **kw: Any) -> None:
            events.append(f"started {Path(cmd[0]).name}")

        def terminate(self) -> None:
            events.append("terminated")

        def wait(self, timeout: float) -> None:
            events.append("waited")

    clean_env.setattr(sys, "platform", "linux")
    clean_env.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/Xvfb")
    clean_env.setattr(subprocess, "Popen", FakeXvfb)
    clean_env.setitem(sys.modules, "tkinter", DeadTk("tkinter"))
    assert conftest._choose_gui_backend() == "stub"
    assert events == ["started Xvfb", "pipe closed", "terminated", "waited"] and "DISPLAY" not in os.environ and conftest._XVFB is None


def test_choose_backend_keeps_the_xvfb_display_when_tk_opens(clean_env: pytest.MonkeyPatch) -> None:
    class FakeXvfb:
        stdout = types.SimpleNamespace(readline=lambda: "77\n", close=lambda: None)

        def __init__(self, cmd: list[str], **kw: Any) -> None:
            pass

    clean_env.setattr(sys, "platform", "linux")
    clean_env.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/Xvfb")
    clean_env.setattr(subprocess, "Popen", FakeXvfb)
    clean_env.setitem(sys.modules, "tkinter", LiveTk("tkinter"))
    assert conftest._choose_gui_backend() == "real" and os.environ["DISPLAY"] == ":77"
    clean_env.setattr(conftest, "_XVFB", None)  # the fake never ran a process


# ---- platform logic of the selector, deterministic on any host --------------------------------------------------------------------------------
@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_native_tk_platforms_need_neither_display_nor_xvfb(clean_env: pytest.MonkeyPatch, platform: str) -> None:
    def forbidden(*a: Any, **k: Any) -> Any:
        raise AssertionError("Xvfb must never be started on this platform")

    clean_env.setattr(sys, "platform", platform)
    clean_env.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/Xvfb")  # present, and still not used
    clean_env.setattr(subprocess, "Popen", forbidden)
    clean_env.setitem(sys.modules, "tkinter", LiveTk("tkinter"))
    assert conftest._choose_gui_backend() == "real" and "DISPLAY" not in os.environ
    clean_env.setitem(sys.modules, "tkinter", DeadTk("tkinter"))
    assert conftest._choose_gui_backend() == "stub"  # auto still degrades when the native Tk is broken


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_backend_real_never_falls_back(clean_env: pytest.MonkeyPatch, platform: str) -> None:
    clean_env.setattr(sys, "platform", platform)
    clean_env.setattr(shutil, "which", lambda name, *a, **k: None)
    clean_env.setenv("DBLENS_TK_BACKEND", "real")
    clean_env.setitem(sys.modules, "tkinter", DeadTk("tkinter"))
    with pytest.raises(DeadTk.TclError):
        conftest._choose_gui_backend()
    clean_env.setitem(sys.modules, "tkinter", None)
    with pytest.raises(ImportError):
        conftest._choose_gui_backend()
    clean_env.setitem(sys.modules, "tkinter", LiveTk("tkinter"))
    clean_env.setenv("DISPLAY", ":1")
    assert conftest._choose_gui_backend() == "real"


@pytest.mark.parametrize("variable", [("DBLENS_TK_BACKEND", "stub"), ("DBLENS_TK_BACKEND", "STUB"), ("DBLENS_TK_STUB", "1")])
def test_stub_switches_win_even_when_a_real_tk_would_open(clean_env: pytest.MonkeyPatch, variable: tuple[str, str]) -> None:
    clean_env.setitem(sys.modules, "tkinter", LiveTk("tkinter"))
    clean_env.setenv(*variable)
    assert conftest._choose_gui_backend() == "stub"
