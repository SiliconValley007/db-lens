"""The scripted version of TESTING.md must keep passing, so the manual plan and the code cannot drift apart."""

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke_test.py"


def test_smoke_script_passes_end_to_end(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("DBLENS_") and k != "DB_LENS_CONFIG"}
    report = tmp_path / "report.md"
    p = subprocess.run([sys.executable, str(SCRIPT), "--report", str(report)], capture_output=True, text=True, encoding="utf-8", env=env, timeout=600)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-1000:]
    assert " 0 failed" in p.stdout and "| FAIL |" not in report.read_text(encoding="utf-8")
    assert "| 2.7 | PASS |" in report.read_text(encoding="utf-8")


def test_smoke_script_help_and_usage_error() -> None:
    p = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0 and "--with-packaging" in p.stdout and "--live" in p.stdout
    assert subprocess.run([sys.executable, str(SCRIPT), "--nope"], capture_output=True).returncode == 2
