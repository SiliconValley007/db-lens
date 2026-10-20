"""Scriptable stand-ins for a SQLAlchemy Engine/Connection: record every statement, replay canned rows or errors.
These fakes are the ONLY verification Oracle (and the probe logic for other engines' error paths) gets: no live Oracle server
was available. They prove statement text, bind parameters, ordering and failure handling, not real Oracle behaviour."""

from __future__ import annotations

import stat
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError


class FakeResult:
    def __init__(self, rows: Sequence[Sequence[Any]], columns: Sequence[str] = ("c",)) -> None:
        self.rows, self.columns, self.closed, self.fetched = rows, list(columns), False, 0

    def keys(self) -> list[str]:
        return self.columns

    def fetchmany(self, n: int | None = None) -> list[Sequence[Any]]:
        out = list(self.rows if n is None else self.rows[:n])
        self.fetched += len(out)
        return out

    def first(self) -> tuple[Any, ...] | None:
        return tuple(self.rows[0]) if self.rows else None

    def __enter__(self) -> FakeResult:
        return self

    def __exit__(self, *a: Any) -> Literal[False]:
        self.closed = True
        return False

    def scalar(self) -> Any:
        return self.rows[0][0] if self.rows else None

    def one(self) -> tuple[Any, ...]:
        return tuple(self.rows[0])

    def all(self) -> Sequence[Sequence[Any]]:
        return self.rows

    def __iter__(self) -> Iterator[Sequence[Any]]:
        return iter(self.rows)


class FakeConn:
    """`script` is a list of (substring-of-SQL, rows | Exception); first match wins, default is a single 0 row."""

    def __init__(self, dialect: Any, script: Sequence[tuple[str, Any]] = (), driver_connection: Any = None) -> None:
        self.dialect = dialect
        self.script: list[tuple[str, Any]] = list(script)
        self.log: list[tuple[str, Any]] = []
        self.options: list[dict[str, Any]] = []
        self.exec_kw: list[dict[str, Any]] = []
        self.rollbacks = 0
        self.info: dict[str, Any] = {}
        self.invalidated = False
        self.connection = SimpleNamespace(driver_connection=driver_connection or SimpleNamespace(call_timeout=None))

    @property
    def sql(self) -> list[str]:
        return [q for q, _ in self.log]

    def execute(self, q: Any, params: Any = None, **kw: Any) -> FakeResult:
        self.log.append((str(q), params))
        self.exec_kw.append(kw)
        for needle, outcome in self.script:
            if needle in str(q):
                if isinstance(outcome, Exception):
                    raise outcome
                return FakeResult(outcome)
        return FakeResult([(0,)])

    def execution_options(self, **kw: Any) -> FakeConn:
        self.options.append(kw)
        return self

    def rollback(self) -> None:
        self.rollbacks += 1

    def invalidate(self) -> None:
        self.invalidated = True

    def __enter__(self) -> FakeConn:
        return self

    def __exit__(self, *a: Any) -> Literal[False]:
        return False


class FakeEngine:
    def __init__(self, conn: FakeConn, database: str | None = None) -> None:
        self.conn, self.dialect, self.url = conn, conn.dialect, SimpleNamespace(database=database)

    def connect(self) -> FakeConn:
        return self.conn


def new_dialect(module: Any) -> Any:
    """Instantiate a SQLAlchemy dialect (``postgresql.dialect()``): its constructor is untyped upstream, so it is called through Any here."""
    return module.dialect()


def fc(conn: object) -> FakeConn:
    """View a Connection that came out of db.connect_ro(<fake engine>) as the FakeConn it really is."""
    return cast(FakeConn, conn)


def as_real(fake: object) -> Any:
    """Hand a fake engine/connection to code typed for the real thing (they are duck-typed stand-ins)."""
    return cast(Connection, fake)


class OraError(SQLAlchemyError):
    """An error shaped like the driver's: str(e.orig) carries the ORA- code."""

    orig: BaseException


def ora(code: int, msg: str = "failure") -> OraError:
    e = OraError(f"ORA-{code:05d}: {msg}")
    e.orig = e
    return e


def is_posix() -> bool:
    return sys.platform != "win32"


def assert_mode(path: Path, mode: int) -> None:
    """POSIX: the exact permission bits. Windows (NTFS ACLs): only existence; the syscall-level tests in test_store.py prove the calls."""
    assert path.exists()
    if is_posix():
        assert stat.S_IMODE(path.stat().st_mode) == mode
