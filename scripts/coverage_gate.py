#!/usr/bin/env python3
"""Coverage gate: total >= 98.5 % and every db_lens module >= 96 %.  Usage: pytest && coverage json -o coverage.json && python scripts/coverage_gate.py"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOTAL, MODULE = 98.5, 96.0


def failures(report: dict[str, object], total: float = TOTAL, module: float = MODULE) -> list[str]:
    out: list[str] = []
    files = report["files"]
    assert isinstance(files, dict)
    for name, data in sorted(files.items()):
        pct = float(data["summary"]["percent_covered"])
        if pct < module:
            out.append(f"{name}: {pct:.2f}% < {module}%")
    pct_total = float(report["totals"]["percent_covered"])  # type: ignore[index]
    if pct_total < total:
        out.append(f"TOTAL: {pct_total:.2f}% < {total}%")
    return out


def main(path: str = "coverage.json") -> int:
    found = failures(json.loads(Path(path).read_text(encoding="utf-8")))
    for line in found:
        print(f"coverage gate FAILED: {line}")
    if not found:
        print(f"coverage gate OK: total >= {TOTAL}%, every module >= {MODULE}%")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
