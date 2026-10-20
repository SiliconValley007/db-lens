"""Presentation-neutral helpers shared by front ends: safe cell text and atomic, formula-safe result export."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import cli

EXPORT_FORMATS = ("csv", "json")


def display(value: object, max_cell: int = 300) -> str:
    """Printable text for one database value: control bytes become visible ``\\xNN``; long text is cut with an ellipsis."""
    text = cli.clean(value)
    return text if len(text) <= max_cell else text[: max(1, max_cell - 1)] + "…"


def export_rows(path: str | os.PathLike[str], fmt: str, columns: list[str], rows: list[list[Any]]) -> None:
    """Write ``rows`` to ``path`` as CSV (spreadsheet formulas neutralised) or JSON. Atomic; the file is private (0600 on POSIX)."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"Unsupported export format {fmt!r}; use one of: {', '.join(EXPORT_FORMATS)}.")
    target = Path(path)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".export-", suffix=".tmp")  # mkstemp creates the file 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            if fmt == "csv":
                w = cli.csv_writer(f)
                w.writerow([cli.csv_safe(c) for c in columns])
                w.writerows([cli.csv_safe(v) for v in r] for r in rows)
            else:
                json.dump([dict(zip(columns, r)) for r in rows], f, default=str, indent=2)
                f.write("\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
