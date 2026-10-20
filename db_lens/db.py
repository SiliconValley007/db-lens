"""Engines, read-only sessions, schema resolution, catalog metrics and account probing.

ENGINE COVERAGE: what was run against a live server, and what was not.
  LIVE (tests/test_integration.py): SQLite; PostgreSQL 16 via psycopg2; MariaDB 10.11 via PyMySQL, mysqlclient and mysql-connector
    (read-only sessions, timeouts, early-stop teardown with server-uptime checks, role/routine probing, error codes).
  MOCK ONLY (tests/fakes.py + tests/test_oracle_mock.py): Oracle. Statement text, bind parameters, ordering, case-folding and
    failure paths are checked against scripted replies; the SQL has NEVER run on a real Oracle server.
  NOT TESTED: Oracle's own MySQL Server (5.7/8.x/9.x). It shares the MariaDB code path, but these differences are unverified:
    the read-only variable (transaction_read_only vs tx_read_only; both are tried), the timeout (max_execution_time in
    milliseconds, SELECT only, vs MariaDB's max_statement_time in seconds), role discovery (MariaDB's
    information_schema.applicable_roles may be absent: roles are then reported as unresolved, never assumed safe),
    SHOW GRANTS output, and routine metadata.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple

from sqlalchemy import Connection, Engine, create_engine, func, literal, select, table, text, union_all
from sqlalchemy.engine import URL
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.exc import SQLAlchemyError, StatementError
from sqlalchemy.pool import NullPool

log = logging.getLogger("db_lens")
log.addHandler(logging.NullHandler())  # library etiquette: the CLI prints notices itself; no stderr duplicates via lastResort


class Notice(NamedTuple):
    """A non-fatal condition the user should know about (structured: machine-readable code + human message)."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.message} [{self.code}]"


_ORA = re.compile(r"\b(ORA|DPY|DPI|TNS)-(\d{3,5})\b")
_MYSQL_MODULES = {"pymysql", "MySQLdb", "mysql", "_mysql_exceptions"}


def error_code(e: BaseException) -> str | None:
    """Normalise a driver error to 'ORA-01031' / 'MY-1044' / 'SQLSTATE-42501' (None if unknown), whatever the driver."""
    o = getattr(e, "orig", None) or e
    if m := _ORA.search(str(o)):
        return f"{m[1]}-{m[2]}"
    if type(o).__module__.split(".")[0] in _MYSQL_MODULES:
        errno = getattr(o, "errno", None)
        if not isinstance(errno, int) and o.args and isinstance(o.args[0], int):
            errno = o.args[0]
        if isinstance(errno, int):
            return f"MY-{errno}"
    if v := getattr(o, "pgcode", None) or getattr(o, "sqlstate", None):
        return f"SQLSTATE-{v}"
    return None


def _notice(c: Connection, code: str, message: str) -> None:
    n = Notice(code, message)
    c.info.setdefault("warnings", []).append(n)
    log.warning("%s", n, extra={"code": code})


def flavor(dialect: Any) -> str:
    """Dialect name, but 'mariadb' for a MariaDB server reached through a mysql:// URL (different timeout variable etc.)."""
    return "mariadb" if getattr(dialect, "is_mariadb", False) is True else dialect.name


_T = {"connect_timeout": 5}
CONNECT_ARGS = {"postgresql": _T, "mysql": _T, "mariadb": _T, "oracle": {"tcp_connect_timeout": 5}}
# Session-level read-only (PostgreSQL uses SQLAlchemy's `postgresql_readonly` execution option instead).
RO = {
    "sqlite": "PRAGMA query_only=ON",
    "mysql": "SET SESSION TRANSACTION READ ONLY",
    "mariadb": "SET SESSION TRANSACTION READ ONLY",
    "oracle": "SET TRANSACTION READ ONLY",
}
SCHEMA = {"postgresql": "SET search_path TO {}", "mysql": "USE {}", "mariadb": "USE {}", "oracle": "ALTER SESSION SET CURRENT_SCHEMA = {}"}
TMO = {
    "postgresql": lambda ms: f"SET statement_timeout = {ms}",
    "mysql": lambda ms: f"SET SESSION max_execution_time = {ms}",
    "mariadb": lambda ms: f"SET SESSION max_statement_time = {ms / 1000}",
}
_MY = (
    "SELECT table_name, table_rows, data_length + index_length FROM information_schema.tables WHERE table_schema = :s AND table_type = 'BASE TABLE'",
)
# name, estimated rows, bytes. Tried in order; a failure (e.g. missing privileges) falls through to the next tier.
# :s is the dialect-denormalized schema name (Oracle: 'scott' -> 'SCOTT', quoted mixed-case stays exact).
EST: dict[str, tuple[str, ...]] = {
    "postgresql": (
        "SELECT c.relname, NULLIF(c.reltuples, -1)::bigint, pg_total_relation_size(c.oid) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :s AND c.relkind IN ('r', 'p')",
    ),
    "mysql": _MY,
    "mariadb": _MY,
    "oracle": (
        "SELECT t.table_name, t.num_rows, (SELECT SUM(g.bytes) FROM all_segments g WHERE g.owner = t.owner "
        "AND g.segment_name = t.table_name) FROM all_tables t WHERE t.owner = :s AND t.table_name NOT LIKE 'BIN$%'",
        "SELECT table_name, num_rows, NULL FROM all_tables WHERE owner = :s AND table_name NOT LIKE 'BIN$%'",
        # no ALL_* views at all (very restricted account): its own tables are still visible through USER_TABLES
        "SELECT table_name, num_rows, NULL FROM user_tables WHERE :s = USER AND table_name NOT LIKE 'BIN$%'",
    ),
}
# Queries proving the read-only flag is actually in effect (first one the server understands wins). Oracle exposes no
# unprivileged check; there a failed SET TRANSACTION (ORA-01453 unless first in the transaction) is the only signal.
VERIFY = {
    "sqlite": ("PRAGMA query_only",),
    "postgresql": ("SHOW transaction_read_only",),
    "mysql": ("SELECT @@session.transaction_read_only", "SELECT @@session.tx_read_only"),
    "mariadb": ("SELECT @@session.tx_read_only", "SELECT @@session.transaction_read_only"),
}
# Degrade to a Notice instead of refusing: a missing timeout feature (old MySQL max_execution_time, Oracle driver without
# call_timeout) must not block every query. The READ ONLY session is never in this list: it always fails closed.
TIMEOUT_NONFATAL = {"mysql", "mariadb", "oracle"}
# Schema switch denied for lack of privilege: continue in the account's default schema, loudly. Anything else (unknown
# schema, syntax) stays fatal because the user would otherwise query something they did not ask for.
SCHEMA_NONFATAL = {"oracle": {"ORA-01031"}, "mysql": {"MY-1044"}, "mariadb": {"MY-1044"}}
SQLITE_SIZES = "SELECT m.tbl_name, SUM(d.pgsize) FROM dbstat d JOIN {s}.sqlite_master m ON m.name = d.name WHERE d.schema = :s GROUP BY m.tbl_name"


def enable_connector_streaming(dialect: Any) -> None:
    """mysql-connector buffers every result client-side unless an *unbuffered* cursor is requested, so --limit would download
    the whole table. SQLAlchemy >= 2.1 implements create_server_side_cursor but leaves the flag off; 2.0.x lacks the method
    (every streamed execute raises NotImplementedError). The C-extension build (the default in recent wheels) additionally
    ignores ``cursor(buffered=False)`` while the connection-level ``_buffered`` is True, which SQLAlchemy's connect sets. So:
    opt in, supply the method where missing, and clear ``_buffered`` for the duration of the cursor creation only.
    If any of this fails, fetch_limited() falls back to a buffered cursor."""
    try:
        base: Any = dialect.execution_ctx_cls
        native = base.create_server_side_cursor is not DefaultExecutionContext.create_server_side_cursor

        def create_server_side_cursor(self: Any) -> Any:
            raw: Any = getattr(self._dbapi_connection, "driver_connection", None)
            was_buffered = getattr(raw, "_buffered", None) is True
            if was_buffered:
                raw._buffered = False
            try:
                return base.create_server_side_cursor(self) if native else self._dbapi_connection.cursor(buffered=False)
            finally:
                if was_buffered:
                    raw._buffered = True

        dialect.execution_ctx_cls = type("StreamingContext", (base,), {"create_server_side_cursor": create_server_side_cursor})
        dialect.supports_server_side_cursors = True
    except Exception:  # noqa: BLE001 - streaming is an optimisation, never a reason to fail engine creation
        pass


_CONNECTOR_BOOLS = frozenset({"ssl_disabled", "ssl_verify_cert", "ssl_verify_identity"})


def _typed_query(u: URL) -> tuple[URL, dict[str, Any]]:
    """URL query values are strings, but the mysql-connector C extension insists on real bools for its TLS switches
    (SQLAlchemy 2.0.x converts only some of them): move those out of the URL into typed connect arguments."""
    if not u.drivername.endswith("+mysqlconnector"):
        return u, {}
    keys = [k for k in u.query if k in _CONNECTOR_BOOLS]
    typed = {k: str(u.query[k]).lower() in ("1", "true", "yes", "on") for k in keys}
    return (u.difference_update_query(keys) if keys else u), typed


def make_engine(u: URL) -> Engine:
    # NullPool: every connection is closed on release, so session state (read-only flag, timeouts, search_path) never leaks.
    u, typed = _typed_query(u)
    eng = create_engine(u, poolclass=NullPool, connect_args={**CONNECT_ARGS.get(u.get_backend_name(), {}), **typed})
    if u.drivername.endswith("+mysqlconnector"):
        enable_connector_streaming(eng.dialect)
    return eng


@contextmanager
def engine(u: URL) -> Iterator[Engine]:
    eng = make_engine(u)
    try:
        yield eng
    finally:
        eng.dispose()


class SessionError(RuntimeError):
    """The read-only/schema/timeout session could not be established or verified; nothing was executed."""


def _init_session(c: Connection, schema: str | None, timeout: float) -> None:
    d, p = flavor(c.dialect), c.dialect.identifier_preparer

    def run(sql: str, what: str, nonfatal: frozenset[str] | set[str] = frozenset()) -> bool:
        log.debug("session: %s", sql)
        try:
            c.execute(text(sql))
            return True
        except SQLAlchemyError as e:
            code = error_code(e)
            if code in nonfatal:  # privilege problem on a convenience setting: warn, keep the transaction clean, go on
                c.rollback()
                return False
            tag = f" ({code})" if code else ""
            raise SessionError(f"Could not {what} on {d}{tag}: {str(getattr(e, 'orig', e)).strip()[:200]}. Refusing to run unprotected.") from e

    if schema and (q := SCHEMA.get(d)):
        if not run(q.format(p.quote_schema(schema)), f"switch to schema {schema!r}", SCHEMA_NONFATAL.get(d, set())):
            _notice(
                c, "SCHEMA-NOT-SWITCHED", f"not permitted to switch to schema {schema!r}; unqualified names resolve in the account's default schema"
            )
    if q := RO.get(d):
        run(q, "enable the read-only session")  # never degraded
    for q in VERIFY.get(d, ()):  # fail closed: the flag must demonstrably be on
        try:
            on = c.execute(text(q)).scalar()
        except SQLAlchemyError:
            c.rollback() if d == "postgresql" else None
            continue
        log.debug("session: read-only check %s -> %r", q, on)
        if str(on).strip().lower() not in {"1", "on", "true"}:
            raise SessionError(f"Read-only session is not in effect on {d} ({q} -> {on!r}). Refusing to run.")
        break
    else:
        if d in VERIFY:
            raise SessionError(f"Could not confirm the read-only session on {d}. Refusing to run.")
    if timeout:
        try:
            ms = int(timeout * 1000)
            raw: Any = c.connection.driver_connection
            if f := TMO.get(d):
                c.execute(text(f(ms)))
            elif d == "oracle":
                raw.call_timeout = ms
            elif d == "sqlite":
                end = time.monotonic() + timeout
                raw.set_progress_handler(lambda: time.monotonic() > end, 1000)
        except (SQLAlchemyError, AttributeError) as e:
            if d not in TIMEOUT_NONFATAL:
                raise SessionError(f"Could not set the statement timeout on {d}: {e}") from e
            if isinstance(e, SQLAlchemyError):
                c.rollback()
            _notice(c, "TIMEOUT-UNSUPPORTED", f"statement timeout is not supported by this {d} server/driver; --timeout ignored")


@contextmanager
def connect_ro(eng: Engine, schema: str | None = None, timeout: float = 0) -> Iterator[Connection]:
    """Connection with schema scoping, a timeout, and read-only enforced AND verified by the database itself.
    Non-fatal problems are left in ``c.info["warnings"]`` as Notice objects; fatal ones raise SessionError before any
    user SQL runs."""
    with eng.connect() as c:
        if eng.dialect.name == "postgresql":
            c = c.execution_options(postgresql_readonly=True)
        _init_session(c, schema, timeout)
        yield c


class Discard(NamedTuple):
    method: str  # "driver" (all cleanup steps worked) | "generic" (some driver internals unavailable) | "failed" (not discarded)
    steps: tuple[str, ...] = ()


def _tidy_pymysql(raw: Any, cursor: Any) -> None:
    cursor._result = None  # SSCursor.close() would read every remaining row
    if (pending := getattr(raw, "_result", None)) is not None:
        pending.unbuffered_active = False  # MySQLResult.__del__ would otherwise try to read from the closed socket


def _tidy_mysqldb(raw: Any, cursor: Any) -> None:
    cursor.connection = None  # Cursor.close() returns early without a connection (else _discard() reads the rest)


def _tidy_connector(raw: Any, cursor: Any) -> None:
    cursor._connection = None  # close() would call consume_results()


# Optional per-driver tidy-up of private state, purely cosmetic (no drain, no 'Exception ignored' noise at GC). A driver
# update that renames these only loses the tidy-up; hard_close() below never depends on them.
def _connector_shutdown(raw: Any) -> None:
    raw.shutdown()  # pure-Python MySQLConnection: documented as "closes the socket, no QUIT, no exceptions"


def _connector_cext_close(raw: Any) -> None:
    # CMySQLConnection.shutdown() is not implemented and its close() first reads every pending row (free_result()). The C handle's
    # own close() is mysql_close(): it drops the connection without reading (and is NOT mysql_shutdown()).
    raw._cmysql.close()
    raw._cmysql = None  # makes CMySQLConnection.close() a no-op afterwards


# (SQLAlchemy driver, exact class module) -> (step name, function). The ONLY places a raw connection method is invoked on top of
# the generic invalidate(); each is verified against a live server and guarded by the server-uptime checks in the test-suite.
SOCKET_CLOSERS = {
    ("mysqlconnector", "mysql.connector.connection"): ("shutdown", _connector_shutdown),
    ("mysqlconnector", "mysql.connector.connection_cext"): ("cext-close", _connector_cext_close),
}
TIDY = {"pymysql": _tidy_pymysql, "mysqldb": _tidy_mysqldb, "mysqlconnector": _tidy_connector}


def hard_close(c: Connection, res: Any) -> Discard:
    """Driver-agnostic teardown of a half-read MySQL/MariaDB stream: stop the cursor from draining, then drop the
    connection (the server aborts the statement). Uses only duck-typed public behaviour first and never raises:
      1. neuter ``cursor.close`` (it would read every unread row);
      2. a socket-level close for mysql-connector only (SOCKET_CLOSERS: pure-Python ``shutdown()`` or the C handle's close);
      3. best-effort private tidy-up for known drivers (TIDY);
      4. ``Connection.invalidate()``: closes the DBAPI connection and removes it from reuse.
    If the connection cannot be invalidated the cursor is restored, so a normal (slow, correct) close still happens."""
    cursor, steps = getattr(res, "cursor", None), []
    try:
        raw = c.connection.driver_connection
    except Exception:  # noqa: BLE001
        raw = None
    if cursor is not None:
        try:
            cursor.close = lambda *a, **k: None
            steps.append("neuter-close")
        except Exception:  # noqa: BLE001 - C-extension cursors may refuse attributes
            pass
    # NEVER duck-type this: on mysqlclient, Connection.shutdown() is mysql_shutdown() and stops the DATABASE SERVER. Low-level
    # closers are looked up by (driver, exact class module); anything not listed (mysqlclient, PyMySQL, unknown drivers, a
    # lookalike class) goes through invalidate() = the driver's ordinary close(), which sends QUIT and never SHUTDOWN.
    if raw is not None and (closer := SOCKET_CLOSERS.get((c.dialect.driver, type(raw).__module__))):
        try:
            closer[1](raw)
            steps.append(closer[0])
        except Exception:  # noqa: BLE001
            pass
    if (tidy := TIDY.get(getattr(c.dialect, "driver", ""))) and cursor is not None:
        try:
            tidy(raw, cursor)
            steps.append("tidy")
        except Exception:  # noqa: BLE001 - AttributeError & co: driver internals changed
            pass
    try:
        c.invalidate()
    except Exception:  # noqa: BLE001
        if "neuter-close" in steps:
            try:
                del cursor.close  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
        return Discard("failed", tuple(steps))
    return Discard("driver" if "tidy" in steps else "generic", tuple(steps))


def fetch_limited(
    c: Connection, stmt: Any, limit: int, stream: bool = False, on_discard: Callable[[Discard], None] | None = None
) -> tuple[list[Any], list[Any], bool]:
    """Run ``stmt`` and return (columns, up to ``limit`` rows, truncated). The result is always closed.
    MySQL drivers read every unread row when a cursor is closed, costing time proportional to the table; on an early stop
    the connection is dropped instead (hard_close) and ``on_discard`` receives the Discard report. The connection is
    unusable afterwards. ``stream`` uses a server-side cursor for this statement only: session/verification statements
    such as SHOW cannot run on PostgreSQL named cursors."""
    try:
        result = c.execute(stmt, execution_options={"stream_results": True} if stream else None)
    except StatementError as e:
        if not (stream and isinstance(e.orig, NotImplementedError)):
            raise
        c.dialect.supports_server_side_cursors = False  # this SQLAlchemy/driver pair cannot stream: remember, run buffered
        result = c.execute(stmt)
    with result as res:
        cols, rows = list(res.keys()), res.fetchmany(limit + 1)
        trunc = len(rows) > limit
        if trunc and c.dialect.name in ("mysql", "mariadb"):
            report = hard_close(c, res)
            log.debug("early stop: connection discarded (%s: %s)", report.method, ", ".join(report.steps))
            if on_discard:
                on_discard(report)
    return cols, list(rows[:limit]), trunc


def _match(name: str, options: list[str]) -> str | None:
    if name in options:
        return name
    hits = [o for o in options if o.lower() == name.lower()]
    if len(hits) > 1:
        raise ValueError(f"{name!r} is ambiguous (matches {', '.join(map(repr, hits))}); use the exact name.")
    return hits[0] if hits else None


def resolve_schema(insp: Inspector, name: str | None) -> str | None:
    if not name:
        return insp.default_schema_name
    try:
        names = insp.get_schema_names()
    except (NotImplementedError, SQLAlchemyError):
        return name
    if (m := _match(name, names)) is None:
        raise ValueError(f"Schema {name!r} not found. Available: {', '.join(names[:15])}{' …' if len(names) > 15 else ''}")
    return m


def resolve_table(insp: Inspector, name: str, sch: str | None) -> str:
    names = insp.get_table_names(schema=sch) + insp.get_view_names(schema=sch)
    if (m := _match(name, names)) is None:
        raise ValueError(f"Table {name!r} not found in schema {sch!r}.")
    return m


def column_counts(insp: Inspector, sch: str | None, names: list[str]) -> dict[str, int]:
    """Bulk column counts in chunks (Oracle caps IN lists at 1000); {} if reflection is not permitted."""
    out: dict[str, int] = {}
    try:
        for i in range(0, len(names), 500):
            out.update({k[1]: len(v) for k, v in insp.get_multi_columns(schema=sch, filter_names=names[i : i + 500]).items()})
    except (NotImplementedError, SQLAlchemyError):
        pass
    return out


def _catalog(c: Connection, sch: str | None) -> dict[str, tuple[Any, Any]]:
    p = c.dialect
    # Only dialects that fold identifiers (Oracle) may normalise: SQLAlchemy's default denormalize_name would turn
    # PostgreSQL's 'public' into 'PUBLIC' and no catalog row would ever match.
    fold = getattr(p, "requires_name_normalize", False)

    def norm(n: str) -> Any:
        return p.normalize_name(n) if fold else n

    def denorm(n: str | None) -> Any:
        return p.denormalize_name(n) if fold and n is not None else n

    for q in EST[p.name]:
        try:
            return {norm(n): (r, s) for n, r, s in c.execute(text(q), {"s": denorm(sch)})}
        except (SQLAlchemyError, ValueError, TypeError):  # driver error, or an unexpected row shape from an odd catalog
            c.rollback()  # a failed statement poisons the PostgreSQL transaction; try the next tier
    return {}


def _counts(c: Connection, sch: str | None, names: list[str]) -> dict[str, tuple[Any, Any]]:
    """Exact counts, one UNION ALL per 100 tables; a failing batch degrades to per-table queries."""
    d: dict[str, tuple[Any, Any]] = {}
    for i in range(0, len(names), 100):
        batch = names[i : i + 100]
        try:
            sub = [select(literal(n), func.count()).select_from(table(n, schema=sch)) for n in batch]
            d.update({n: (r, None) for n, r in c.execute(union_all(*sub))})
        except SQLAlchemyError:
            c.rollback()
            for n in batch:
                try:
                    d[n] = (c.execute(select(func.count()).select_from(table(n, schema=sch))).scalar(), None)
                except SQLAlchemyError:
                    c.rollback()
    return d


def _sqlite_sizes(c: Connection, sch: str | None) -> dict[str, int]:
    q = SQLITE_SIZES.format(s=c.dialect.identifier_preparer.quote_schema(sch or "main"))
    try:
        return {str(r[0]): int(r[1]) for r in c.execute(text(q), {"s": sch or "main"})}  # needs SQLITE_ENABLE_DBSTAT_VTAB
    except SQLAlchemyError:
        return {}


def estimates(eng: Engine, sch: str | None, names: list[str]) -> dict[str, tuple[int | None, int | None]]:
    """name -> (rows, bytes). Never raises: any failure just leaves the metric out."""
    try:
        with eng.connect() as c:
            if eng.dialect.name in EST:
                return _catalog(c, sch)
            d = _counts(c, sch, names)
            if eng.dialect.name == "sqlite":
                sizes = _sqlite_sizes(c, sch)
                d = {n: (r, sizes.get(n)) for n, (r, _) in d.items()}
            return d
    except SQLAlchemyError:
        return {}


def sqlite_summary(eng: Engine, sch: str | None) -> str:
    q = eng.dialect.identifier_preparer.quote_schema(sch or "main")
    with eng.connect() as c:
        n, sz, free = (c.execute(text(f"PRAGMA {q}.{k}")).scalar() or 0 for k in ("page_count", "page_size", "freelist_count"))
    return f"{n:,} pages × {sz:,} B · {n * sz:,} B total · {free:,} free pages"


WRITE_PRIVS = {
    "ALL",
    "ALL PRIVILEGES",
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "SUPER",
    "FILE",
    "TRIGGER",
    "CREATE ROUTINE",
    "ALTER ROUTINE",
    "CREATE VIEW",
    "EVENT",
}
# PostgreSQL: pg_has_role(.., 'MEMBER') covers inherited roles AND NOINHERIT roles reachable with SET ROLE; has_table_privilege
# accepts the role's oid, so the whole hierarchy is resolved server-side.
PG_WRITE = (
    "SELECT EXISTS (SELECT 1 FROM pg_roles r WHERE pg_has_role(current_user, r.oid, 'MEMBER') AND (r.rolsuper "
    "OR r.rolname IN ('pg_write_all_data', 'pg_execute_server_program', 'pg_write_server_files') "
    "OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p') "
    "AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND (has_table_privilege(r.oid, c.oid, 'INSERT') "
    "OR has_table_privilege(r.oid, c.oid, 'UPDATE') OR has_table_privilege(r.oid, c.oid, 'DELETE'))) "
    "OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema' "
    "AND has_schema_privilege(r.oid, n.oid, 'CREATE'))))"
)
# Executable PostgreSQL routines, split into user-created ("custom") and extension-provided. Built-ins (pg_catalog,
# information_schema), temp/toast schemas, aggregates/window functions and trigger functions (not callable from SQL) are
# excluded in SQL. Returns (ext, security_definer, count, sample qualified names, bare names).
_PG_R = (
    "SELECT x.ext, x.definer, count(*), (array_agg(x.fq ORDER BY x.fq))[1:5], (array_agg(x.name ORDER BY x.fq))[1:200] FROM ("
    "SELECT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e') AS ext, "
    "p.prosecdef AS definer, n.nspname || '.' || p.proname AS fq, p.proname AS name FROM pg_proc p "
    "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%' AND {kind} "
    "AND p.prorettype NOT IN ('trigger'::regtype, 'event_trigger'::regtype) AND has_function_privilege(p.oid, 'EXECUTE')) x "
    "GROUP BY x.ext, x.definer"
)
PG_ROUTINES = _PG_R.format(kind="p.prokind IN ('f', 'p')")
PG_ROUTINES_LEGACY = _PG_R.format(kind="NOT p.proisagg AND NOT p.proiswindow")  # PostgreSQL < 11 has no prokind
MY_ROUTINES = (
    "SELECT routine_schema, routine_name, security_type FROM information_schema.routines "
    "WHERE routine_schema NOT IN ('mysql', 'sys', 'information_schema', 'performance_schema') LIMIT 201"
)
MY_ROLES = "SELECT DISTINCT role_name FROM information_schema.applicable_roles"
_SYS = (
    "('SYS', 'SYSTEM', 'XDB', 'MDSYS', 'CTXSYS', 'ORDSYS', 'ORDDATA', 'WMSYS', 'LBACSYS', 'DVSYS', 'OJVMSYS', 'AUDSYS', "
    "'GSMADMIN_INTERNAL', 'APPQOSSYS', 'DBSNMP', 'OUTLN', 'SYSMAN', 'OLAPSYS', 'DIP', 'ANONYMOUS', 'XS$NULL')"
)
ORA_PROBES = (  # (query, kind, meaning): each returns one COUNT. CREATE SESSION is excluded: every account holds it.
    (
        "SELECT COUNT(*) FROM (SELECT privilege FROM session_privs UNION ALL SELECT privilege FROM role_sys_privs) "
        "WHERE (privilege LIKE 'CREATE %' AND privilege <> 'CREATE SESSION') OR privilege LIKE 'DROP %' OR privilege LIKE 'ALTER %' "
        "OR privilege IN ('INSERT ANY TABLE', 'UPDATE ANY TABLE', 'DELETE ANY TABLE', 'EXECUTE ANY PROCEDURE')",
        "write",
        "holds write/DDL system privileges",
    ),
    (
        "SELECT COUNT(*) FROM (SELECT privilege FROM all_tab_privs WHERE grantee IN (USER, 'PUBLIC') UNION ALL "
        "SELECT privilege FROM role_tab_privs) WHERE privilege IN ('INSERT', 'UPDATE', 'DELETE')",
        "write",
        "has INSERT/UPDATE/DELETE on tables",
    ),
    ("SELECT COUNT(*) FROM user_tables WHERE ROWNUM = 1", "write", "owns tables"),
    (
        f"SELECT COUNT(*) FROM (SELECT table_name FROM all_tab_privs WHERE privilege = 'EXECUTE' AND table_schema NOT IN {_SYS} "
        f"AND (grantee IN (USER, 'PUBLIC') OR grantee IN (SELECT role FROM session_roles)) UNION ALL "
        f"SELECT table_name FROM role_tab_privs WHERE privilege = 'EXECUTE' AND owner NOT IN {_SYS} UNION ALL "
        f"SELECT object_name FROM user_objects WHERE object_type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE'))",
        "execute",
        "user-defined routines",
    ),
    (
        "SELECT COUNT(*) FROM user_role_privs WHERE default_role = 'NO'",
        "roles",
        "granted roles are not enabled by default (SET ROLE could enable them) and cannot be resolved statically",
    ),
)


def mysql_role_grants(grants: list[str]) -> list[str]:
    """Role names granted to the account, read from SHOW GRANTS lines such as GRANT `writer`@`%` TO `u`@`%`."""
    names: list[str] = []
    for g in grants:
        if re.match(r"GRANT\s+`[^`]+`", g, re.I) and not re.search(r"\sON\s", g, re.I):
            names += re.findall(r"`([^`]+)`(?:@`[^`]*`)?", g.split(" TO ")[0])
    return list(dict.fromkeys(names))


def mysql_write_grants(grants: list[str]) -> str | None:
    """First write-capable privilege found in SHOW GRANTS output (MySQL/MariaDB), else None."""
    for g in grants:
        if m := re.match(r"GRANT (.+?) ON ", g, re.I):
            hit = {x.strip().upper() for x in re.sub(r"\([^)]*\)", "", m[1]).split(",")} & WRITE_PRIVS  # drop column lists
            if hit:
                return sorted(hit)[0]
    return None


class Risk(NamedTuple):
    """Something the read-only guarantees cannot cover. ``blocking`` risks make ``query --strict`` refuse; informational ones
    (e.g. extension-provided functions) are shown but never block."""

    kind: str  # custom-routine | extension-routine | unresolved-role | unknown
    message: str
    blocking: bool = True
    names: tuple[str, ...] = ()  # bare routine names, used to recognise calls to them in a statement

    def __str__(self) -> str:
        return self.message


def _custom_routines(n: int, definer: int, sample: list[str], names: list[str], capped: bool = False) -> Risk:
    more = "+" if capped else ""
    eg = f", e.g. {', '.join(sample)}" if sample else ""
    return Risk(
        "custom-routine",
        f"{n}{more} user-created routine(s) executable by this account ({int(definer or 0)} with "
        f"DEFINER/SECURITY DEFINER rights{eg}) can write regardless of read-only grants; the SQL guard cannot see inside them",
        True,
        tuple(dict.fromkeys(names))[:200],
    )


def _rows(c: Connection, q: str) -> list[Any] | None:
    try:
        return list(c.execute(text(q)))
    except SQLAlchemyError:
        c.rollback()
        return None


Probe = tuple[list[str], list[Risk], list[str]]


def _probe_mysql(c: Connection) -> Probe:
    reasons: list[str] = []
    risks: list[Risk] = []
    failed: list[str] = []
    q = c.dialect.identifier_preparer.quote
    grants = [r[0] for r in c.execute(text("SHOW GRANTS FOR CURRENT_USER()"))]
    if hit := mysql_write_grants(grants):
        reasons.append(f"grants include {hit}")
    unresolved = []
    role_rows = _rows(c, MY_ROLES)
    if role_rows is None and (named := mysql_role_grants(grants)):  # e.g. vanilla MySQL: no applicable_roles view
        risks.append(
            Risk(
                "unresolved-role",
                f"granted role(s) {', '.join(named)} could not be enumerated through information_schema "
                "on this server; privileges obtained through roles are not analysed",
            )
        )
    for role in [r[0] for r in role_rows or []]:  # granted roles are reachable with SET ROLE even when not default
        try:
            hit = mysql_write_grants([r[0] for r in c.execute(text(f"SHOW GRANTS FOR {q(role)}"))])
        except SQLAlchemyError:
            c.rollback()
            unresolved.append(role)
            continue
        if hit:
            reasons.append(f"role {role} grants {hit}")
    if unresolved:
        risks.append(Risk("unresolved-role", f"role(s) {', '.join(unresolved)} could not be resolved statically; their privileges are unknown"))
    if rows := _rows(c, MY_ROUTINES):
        capped = len(rows) > 200
        rows = rows[:200]
        risks.append(
            _custom_routines(len(rows), sum(r[2] == "DEFINER" for r in rows), [f"{r[0]}.{r[1]}" for r in rows[:5]], [r[1] for r in rows], capped)
        )
    return reasons, risks, failed


def _probe_oracle(c: Connection) -> Probe:
    reasons, risks, failed = [], [], []
    for q, kind, why in ORA_PROBES:
        try:
            n = c.execute(text(q)).scalar() or 0
        except SQLAlchemyError:
            c.rollback()
            failed.append(why)
            continue
        if n and kind == "write":
            reasons.append(why)
        elif n and kind == "execute":
            risks.append(
                Risk(
                    "custom-routine",
                    f"{n} {why} can be executed; they can write regardless of read-only grants and the SQL guard cannot see inside them",
                )
            )
        elif n:
            risks.append(Risk("unresolved-role", f"{n} {why}"))
    return reasons, risks, failed


def _probe_postgres(c: Connection) -> Probe:
    reasons, risks, failed = [], [], []
    if c.execute(text(PG_WRITE)).scalar():
        reasons.append("role, or a role it can SET ROLE to, can modify tables/create objects or is privileged")
    rows = _rows(c, PG_ROUTINES)
    if rows is None:
        rows = _rows(c, PG_ROUTINES_LEGACY)
    if rows is None:
        failed.append("executable routines")
    cust = [r for r in rows or [] if not r[0]]
    if cust:
        risks.append(
            _custom_routines(
                sum(r[2] for r in cust), sum(r[2] for r in cust if r[1]), [x for r in cust for x in r[3]][:5], [x for r in cust for x in r[4]]
            )
        )
    if ext := [r for r in rows or [] if r[0]]:
        n = sum(r[2] for r in ext)
        risks.append(
            Risk(
                "extension-routine", f"{n} extension-provided function(s) executable (standard library code, not user-created; informational)", False
            )
        )
    return reasons, risks, failed


def probe_account(eng: Engine) -> tuple[bool | None, str, list[Risk]]:
    """Best-effort audit of the connected account -> (writable True|False|None=unknown, reason, risks).
    Resolves role hierarchies server-side where the database allows (PostgreSQL, MySQL/MariaDB roles, Oracle role views),
    reports what it cannot resolve, and separates user-created routines (blocking: they can write whatever the grants say)
    from standard/extension ones (informational)."""
    d = eng.dialect.name
    if d == "sqlite":
        path = eng.url.database
        if not path or path == ":memory:":
            return None, "in-memory database", []
        return (True, "database file is writable by this OS user", []) if os.access(path, os.W_OK) else (False, "", [])
    probe = {"postgresql": _probe_postgres, "mysql": _probe_mysql, "mariadb": _probe_mysql, "oracle": _probe_oracle}.get(d)
    if probe is None:
        return None, f"unsupported dialect {d}", []
    try:
        with eng.connect() as c:
            reasons, risks, failed = probe(c)
    except SQLAlchemyError as e:
        code = error_code(e)
        return None, f"could not determine{f' ({code})' if code else ''}: {str(getattr(e, 'orig', e)).strip()[:100]}", []
    if reasons:
        return True, "; ".join(reasons), risks
    if failed:
        return None, "could not inspect: " + "; ".join(failed), risks
    return False, "", risks


class TlsError(RuntimeError):
    """TLS was demanded for this profile but the live connection is not encrypted."""


def tls_in_use(eng: Engine) -> bool | None:
    """Ask the server whether *this* connection is encrypted (None = not knowable for this engine)."""
    d = flavor(eng.dialect)
    q = {
        "postgresql": "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()",
        "mysql": "SHOW SESSION STATUS LIKE 'Ssl_cipher'",
        "mariadb": "SHOW SESSION STATUS LIKE 'Ssl_cipher'",
    }.get(d)
    if q is None:
        return None
    with eng.connect() as c:
        row = c.execute(text(q)).first()
    if row is None:
        return False
    return bool(row[0]) if d == "postgresql" else bool(row[1])


def assert_tls(eng: Engine, mode: str) -> None:
    """Fail closed: a profile that demands TLS never talks over an unencrypted connection, whatever the driver's defaults."""
    used = tls_in_use(eng)
    log.debug("tls: required=%s encrypted=%s", mode, used)
    if used is False:
        raise TlsError(
            f"TLS ({mode}) is required for this profile but the connection is NOT encrypted. Check the server's TLS "
            "configuration and the --ssl-* options; nothing was executed."
        )
    if used is None:
        raise TlsError(f"TLS ({mode}) is required but cannot be verified on {eng.dialect.name}; nothing was executed.")
