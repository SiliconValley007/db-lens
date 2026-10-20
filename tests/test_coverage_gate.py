import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "coverage_gate.py"
spec = importlib.util.spec_from_file_location("coverage_gate", SCRIPT)
assert spec is not None and spec.loader is not None
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)


def report(total: float, **files: float) -> dict[str, object]:
    return {"totals": {"percent_covered": total}, "files": {k: {"summary": {"percent_covered": v}} for k, v in files.items()}}


def test_gate_passes_and_names_every_offender() -> None:
    assert cg.failures(report(99.0, a=100.0, b=96.0)) == []
    assert cg.failures(report(98.0, a=95.9, b=99.0)) == ["a: 95.90% < 96.0%", "TOTAL: 98.00% < 98.5%"]


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good, bad = tmp_path / "g.json", tmp_path / "b.json"
    good.write_text(json.dumps(report(99.0, a=99.0)), encoding="utf-8")
    bad.write_text(json.dumps(report(50.0, a=50.0)), encoding="utf-8")
    assert cg.main(str(good)) == 0 and "OK" in capsys.readouterr().out
    assert cg.main(str(bad)) == 1 and "FAILED" in capsys.readouterr().out
