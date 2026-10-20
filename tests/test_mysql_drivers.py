"""hard_close(): driver-agnostic teardown of a half-read MySQL stream. Stubs mimic each driver's cursor/connection internals;
real-driver timing/behaviour is in test_integration.py (needs DBLENS_MYSQL_URL)."""

import importlib
import types
from typing import Any

import pytest
from sqlalchemy import make_url

from db_lens import db

from .fakes import FakeResult, as_real, fc, new_dialect


class Res(FakeResult):
    """Context-manager result like SQLAlchemy's CursorResult; records what close() would find on the driver cursor."""

    def __init__(self, cursor: Any, n: Any = 20) -> None:
        super().__init__([(i,) for i in range(n)])
        self.cursor = cursor
        self.seen_on_close: dict[str, Any] | None = None

    def keys(self) -> Any:
        return ["a"]

    def fetchmany(self, n: int | None = None) -> Any:
        return self.rows[:n]

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *a: Any) -> Any:
        self.seen_on_close = dict(getattr(self.cursor, "__dict__", {})) if self.cursor is not None else {}


def conn(driver: Any, cursor: Any, name: Any = "mysql", raw: Any = None) -> Any:
    c: Any = types.SimpleNamespace(dialect=types.SimpleNamespace(name=name, driver=driver), invalidated=False, events=[])
    c.res = Res(cursor)

    def execute(stmt: Any, **k: Any) -> Any:
        return c.res

    def invalidate() -> None:
        c.events.append("invalidate")
        c.invalidated = True

    c.execute, c.invalidate = execute, invalidate
    c.connection = types.SimpleNamespace(driver_connection=raw)
    return c


def noop_closed(c: Any) -> Any:
    return "close" in c.res.seen_on_close and c.res.seen_on_close["close"]() is None


class NoInternals:
    """A cursor from a driver release that renamed its private state: public attributes still assignable, private ones not."""

    def __setattr__(self, k: Any, v: Any) -> Any:
        if k in {"_result", "connection", "_connection"}:
            raise AttributeError(k)
        object.__setattr__(self, k, v)


class Pending:
    unbuffered_active = True


class Raw:
    __module__ = "mysql.connector.connection"  # shutdown() is only trusted on mysql-connector

    def __init__(self, ev: Any = None, shutdown: Any = True) -> None:
        self.ev, self._result = ev if ev is not None else [], Pending()
        if shutdown:
            self.shutdown = lambda: self.ev.append("shutdown")


def test_pymysql_full_cleanup_no_drain_no_gc_noise() -> None:
    raw = Raw()
    c = conn("pymysql", types.SimpleNamespace(_result=object()), raw=raw)
    reports: list[Any] = []
    _, rows, trunc = db.fetch_limited(c, "q", 5, on_discard=reports.append)
    assert trunc and len(rows) == 5 and c.invalidated
    assert c.res.seen_on_close["_result"] is None and noop_closed(c)
    assert raw._result.unbuffered_active is False  # MySQLResult.__del__ stays silent
    assert reports[0].method == "driver" and "neuter-close" in reports[0].steps


def test_mysqlclient_detaches_connection() -> None:
    c = conn("mysqldb", types.SimpleNamespace(connection=object()), raw=object())
    db.fetch_limited(c, "q", 5)
    assert c.res.seen_on_close["connection"] is None and c.invalidated


def lookalike(module: Any, called: Any) -> Any:
    """A raw connection whose shutdown() would be catastrophic, living in the given module."""
    cls = type("Connection", (), {"shutdown": lambda self: called.append(f"SERVER SHUT DOWN via {module}"), "__module__": module})
    return cls()


@pytest.mark.parametrize(
    "driver,module",
    [
        ("mysqldb", "MySQLdb.connections"),  # mysqlclient: shutdown() == mysql_shutdown() == stops the server
        ("pymysql", "pymysql.connections"),
        ("mariadbconnector", "mariadb.connections"),
        ("somethingnew", "somewhere.else"),
        ("mysqlconnector", "mysql.connector.evil"),
        ("mysqlconnector", "MySQLdb.connections"),  # right driver name, wrong class
        ("mysqldb", "mysql.connector.connection"),  # right class, wrong driver name
        ("mysqldb", "mysql.connector.connection_cext"),
    ],
)
def test_no_low_level_closer_unless_driver_and_class_both_match(driver: Any, module: Any) -> None:
    called: list[Any] = []
    c = conn(driver, types.SimpleNamespace(), raw=lookalike(module, called))
    reports: list[Any] = []
    db.fetch_limited(c, "q", 5, on_discard=reports.append)
    assert called == [] and c.invalidated and not {"shutdown", "cext-close"} & set(reports[0].steps)


def test_closer_allowlist_is_exactly_the_two_mysql_connector_classes() -> None:
    assert set(db.SOCKET_CLOSERS) == {("mysqlconnector", "mysql.connector.connection"), ("mysqlconnector", "mysql.connector.connection_cext")}


def test_cext_connection_is_closed_at_handle_level_never_via_shutdown() -> None:
    """CMySQLConnection has no usable shutdown() and its close() drains: the C handle (mysql_close) is closed directly."""
    ev: list[Any] = []
    handle = types.SimpleNamespace(close=lambda: ev.append("mysql_close"))
    raw = type(
        "CMySQLConnection",
        (),
        {"__module__": "mysql.connector.connection_cext", "_cmysql": handle, "shutdown": lambda self: ev.append("SHUTDOWN CALLED")},
    )()
    c = conn("mysqlconnector", types.SimpleNamespace(_connection=object()), raw=raw)
    reports: list[Any] = []
    db.fetch_limited(c, "q", 5, on_discard=reports.append)
    assert ev == ["mysql_close"] and raw._cmysql is None and "cext-close" in reports[0].steps and c.invalidated


def test_cext_without_private_handle_degrades_to_generic_discard() -> None:
    raw = type("CMySQLConnection", (), {"__module__": "mysql.connector.connection_cext"})()  # no _cmysql: driver changed
    c = conn("mysqlconnector", types.SimpleNamespace(), raw=raw)
    reports: list[Any] = []
    db.fetch_limited(c, "q", 5, on_discard=reports.append)
    assert c.invalidated and "cext-close" not in reports[0].steps


def test_mysql_connector_shuts_socket_down_before_invalidating() -> None:
    raw = Raw()
    c = conn("mysqlconnector", types.SimpleNamespace(_connection=object()), raw=raw)
    raw.ev = c.events
    db.fetch_limited(c, "q", 5)
    assert c.events == ["shutdown", "invalidate"] and c.res.seen_on_close["_connection"] is None


def test_mariadb_dialect_name_is_covered_too() -> None:
    c = conn("pymysql", types.SimpleNamespace(_result=object()), name="mariadb", raw=Raw())
    db.fetch_limited(c, "q", 5)
    assert c.invalidated


@pytest.mark.parametrize("driver", ["pymysql", "mysqldb", "mysqlconnector"])
def test_driver_internals_missing_degrade_to_generic_discard(driver: Any) -> None:
    """A driver update that renames its private attributes must cost only the cosmetic tidy-up: no AttributeError."""
    c = conn(driver, NoInternals(), raw=object())
    reports: list[Any] = []
    _, rows, trunc = db.fetch_limited(c, "q", 5, on_discard=reports.append)
    assert trunc and len(rows) == 5 and c.invalidated
    assert reports[0].method == "generic" and "tidy" not in reports[0].steps and noop_closed(c)


@pytest.mark.parametrize("driver", ["mariadbconnector", "asyncmy", "cymysql", "somethingnew", ""])
def test_unknown_drivers_get_the_generic_discard(driver: Any) -> None:
    c = conn(driver, types.SimpleNamespace(), raw=object())
    reports: list[Any] = []
    db.fetch_limited(c, "q", 5, on_discard=reports.append)
    assert c.invalidated and reports[0].method == "generic" and noop_closed(c)


def test_cursor_refusing_attribute_assignment_still_discards() -> None:
    class Frozen:
        __slots__ = ()

    c = conn("somethingnew", Frozen(), raw=object())
    reports: list[Any] = []
    db.fetch_limited(c, "q", 5, on_discard=reports.append)
    assert c.invalidated and "neuter-close" not in reports[0].steps


def test_raising_shutdown_or_tidy_never_propagates() -> None:
    class Bad:
        def shutdown(self) -> Any:
            raise OSError("socket already closed")

    class Strict:
        def __setattr__(self, k: Any, v: Any) -> Any:
            if k == "_result":
                raise AttributeError(k)
            object.__setattr__(self, k, v)

    c = conn("pymysql", Strict(), raw=Bad())
    reports: list[Any] = []
    db.fetch_limited(c, "q", 5, on_discard=reports.append)
    assert c.invalidated and reports[0].method == "generic" and "shutdown" not in reports[0].steps


def test_invalidate_failure_restores_the_normal_close_and_reports_failed() -> None:
    class Cursor:
        def close(self) -> Any:
            return "real close"

    cur = Cursor()
    c = conn("pymysql", cur, raw=Raw())
    c.invalidate = lambda: (_ for _ in ()).throw(RuntimeError("pool gone"))
    reports: list[Any] = []
    _, rows, trunc = db.fetch_limited(c, "q", 5, on_discard=reports.append)
    assert trunc and len(rows) == 5 and reports[0].method == "failed"
    assert cur.close() == "real close"  # instance override removed again


def test_result_without_cursor_and_connection_without_raw() -> None:
    c = conn("pymysql", None, raw=None)
    c.res.cursor = None
    db.fetch_limited(c, "q", 5)
    assert c.invalidated
    c = conn("pymysql", types.SimpleNamespace(), raw=None)
    c.connection = None  # c.connection.driver_connection raises
    db.fetch_limited(c, "q", 5)
    assert c.invalidated


@pytest.mark.parametrize("name", ["postgresql", "oracle", "sqlite"])
def test_other_dialects_are_never_discarded(name: Any) -> None:
    c = conn("pymysql", types.SimpleNamespace(_result=object()), name=name)
    db.fetch_limited(c, "q", 5)
    assert not c.invalidated


def test_no_discard_when_everything_was_read() -> None:
    c = conn("pymysql", types.SimpleNamespace(_result=object()), raw=Raw())
    c.res = Res(c.res.cursor, n=3)
    reports: list[Any] = []
    _, rows, trunc = db.fetch_limited(c, "q", 5, on_discard=reports.append)
    assert not trunc and len(rows) == 3 and not c.invalidated and reports == []


def test_result_is_closed_even_if_fetch_raises() -> None:
    c = conn("pymysql", types.SimpleNamespace(_result=object()), raw=Raw())
    c.res.fetchmany = lambda n: (_ for _ in ()).throw(RuntimeError("socket reset"))
    with pytest.raises(RuntimeError):
        db.fetch_limited(c, "q", 5)
    assert c.res.seen_on_close is not None and not c.invalidated


def test_tidy_registry_names_match_sqlalchemy_driver_names() -> None:
    assert set(db.TIDY) == {"pymysql", "mysqldb", "mysqlconnector"}
    for drv in db.TIDY:
        assert make_url(f"mysql+{drv}://u:p@h/d").get_dialect().driver == drv


def test_mysqlconnector_engine_opts_into_streaming_others_untouched() -> None:
    importlib.import_module("mysql.connector")  # a dev dependency (requirements-dev.txt): a missing one FAILS, it is never skipped
    from sqlalchemy.dialects import mysql

    assert db.make_engine(make_url("mysql+mysqlconnector://u:p@h/d")).dialect.supports_server_side_cursors is True
    assert db.make_engine(make_url("mariadb+mysqlconnector://u:p@h/d")).dialect.supports_server_side_cursors is True
    assert db.make_engine(make_url("mysql+pymysql://u:p@h/d")).dialect.supports_server_side_cursors is True  # native default
    assert mysql.mysqlconnector.dialect.supports_server_side_cursors is False  # class not mutated


def test_streaming_unsupported_falls_back_to_buffered_once_and_remembers() -> None:
    from sqlalchemy.exc import StatementError

    c = conn("mysqlconnector", types.SimpleNamespace(), raw=object())
    c.dialect.supports_server_side_cursors, calls = True, []
    real = c.execute

    def execute(stmt: Any, **k: Any) -> Any:
        calls.append(k)
        if k.get("execution_options"):
            raise StatementError("not implemented", "q", None, NotImplementedError())  # SQLAlchemy 2.0.x + mysql-connector
        return real(stmt)

    c.execute = execute
    _, rows, trunc = db.fetch_limited(c, "q", 5, stream=True)
    assert len(rows) == 5 and trunc and c.dialect.supports_server_side_cursors is False
    assert [bool(k.get("execution_options")) for k in calls] == [True, False]


def test_other_statement_errors_are_not_swallowed() -> None:
    from sqlalchemy.exc import StatementError

    c = conn("pymysql", types.SimpleNamespace())
    c.execute = lambda stmt, **k: (_ for _ in ()).throw(StatementError("bad", "q", None, ValueError("syntax")))
    with pytest.raises(StatementError):
        db.fetch_limited(c, "q", 5, stream=True)
    c.execute = lambda stmt, **k: (_ for _ in ()).throw(StatementError("bad", "q", None, NotImplementedError()))
    with pytest.raises(StatementError):
        db.fetch_limited(c, "q", 5, stream=False)  # only a *streaming* attempt may fall back


def test_connector_streaming_shim_adds_the_missing_method_to_old_sqlalchemy() -> None:
    """SQLAlchemy 2.0.x has no create_server_side_cursor for mysql-connector (NotImplementedError on every streamed query)."""
    from sqlalchemy.engine.default import DefaultExecutionContext

    class OldContext(DefaultExecutionContext):
        pass

    d = types.SimpleNamespace(execution_ctx_cls=OldContext, supports_server_side_cursors=False)
    db.enable_connector_streaming(d)
    assert d.supports_server_side_cursors is True and issubclass(d.execution_ctx_cls, OldContext) and d.execution_ctx_cls is not OldContext
    seen = {}
    ctx = object.__new__(d.execution_ctx_cls)
    ctx._dbapi_connection = types.SimpleNamespace(cursor=lambda **kw: seen.update(kw) or "unbuffered-cursor", driver_connection=None)
    assert ctx.create_server_side_cursor() == "unbuffered-cursor" and seen == {"buffered": False}
    assert OldContext.create_server_side_cursor is DefaultExecutionContext.create_server_side_cursor  # base class untouched


def test_connector_streaming_shim_wraps_native_implementations_too() -> None:
    class NewContext:
        def create_server_side_cursor(self) -> Any:
            return "native"

    d = types.SimpleNamespace(execution_ctx_cls=NewContext, supports_server_side_cursors=False)
    db.enable_connector_streaming(d)
    ctx = object.__new__(d.execution_ctx_cls)
    ctx._dbapi_connection = types.SimpleNamespace(driver_connection=None)
    assert d.supports_server_side_cursors is True and ctx.create_server_side_cursor() == "native"
    db.enable_connector_streaming(object())  # unexpected dialect shape: swallowed


@pytest.mark.parametrize("native", [True, False])
def test_connector_streaming_clears_connection_level_buffering_only_while_creating_the_cursor(native: Any) -> None:
    """C-extension mysql-connector ignores cursor(buffered=False) while connection._buffered is True (SQLAlchemy sets it)."""
    from sqlalchemy.engine.default import DefaultExecutionContext

    raw = types.SimpleNamespace(_buffered=True)
    seen: list[Any] = []

    class Ctx(DefaultExecutionContext):
        pass

    if native:

        def create_server_side_cursor(self: Any) -> Any:
            seen.append(raw._buffered)
            return "cursor"

        Ctx.create_server_side_cursor = create_server_side_cursor  # type: ignore[method-assign]
    d = types.SimpleNamespace(execution_ctx_cls=Ctx, supports_server_side_cursors=False)
    db.enable_connector_streaming(d)
    ctx = object.__new__(d.execution_ctx_cls)

    def cursor(**kw: Any) -> Any:
        seen.append(raw._buffered)
        return "cursor"

    ctx._dbapi_connection = types.SimpleNamespace(driver_connection=raw, cursor=cursor)
    assert ctx.create_server_side_cursor() == "cursor"
    assert seen == [False] and raw._buffered is True  # unbuffered while creating, restored afterwards
    raw._buffered = False  # already unbuffered: left untouched
    ctx.create_server_side_cursor()
    assert raw._buffered is False


def test_flavor_distinguishes_mariadb_behind_a_mysql_url() -> None:
    assert db.flavor(types.SimpleNamespace(name="mysql", is_mariadb=True)) == "mariadb"
    assert db.flavor(types.SimpleNamespace(name="mysql", is_mariadb=False)) == "mysql"
    assert db.flavor(types.SimpleNamespace(name="mysql")) == "mysql"
    assert db.flavor(types.SimpleNamespace(name="postgresql")) == "postgresql"
    assert db.flavor(types.SimpleNamespace(name="mysql", is_mariadb=object())) == "mysql"  # only a real True counts


@pytest.mark.parametrize(
    "is_mariadb,timeout_sql,verify_first",
    [
        (True, "SET SESSION max_statement_time = 2.0", "SELECT @@session.tx_read_only"),
        (False, "SET SESSION max_execution_time = 2000", "SELECT @@session.transaction_read_only"),
    ],
)
def test_mariadb_behind_mysql_url_gets_mariadb_session_statements(is_mariadb: Any, timeout_sql: Any, verify_first: Any) -> None:
    from sqlalchemy.dialects import mysql

    from .fakes import FakeConn, FakeEngine

    d = new_dialect(mysql)
    d.is_mariadb = is_mariadb
    eng = FakeEngine(FakeConn(d, [("@@session", [(1,)])]))
    with db.connect_ro(as_real(eng), timeout=2) as c:
        assert fc(c).sql == ["SET SESSION TRANSACTION READ ONLY", verify_first, timeout_sql]


def test_connector_tls_switches_become_real_booleans() -> None:
    """mysql-connector's C extension raises 'argument must be bool, not str' for string values coming from a URL query."""
    u = make_url("mysql+mysqlconnector://u:p@h/d?ssl_ca=ca.pem&ssl_verify_cert=true&ssl_verify_identity=TRUE&ssl_disabled=false")
    rest, typed = db._typed_query(u)
    assert typed == {"ssl_verify_cert": True, "ssl_verify_identity": True, "ssl_disabled": False}
    assert dict(rest.query) == {"ssl_ca": "ca.pem"}
    other = make_url("mysql+pymysql://u:p@h/d?ssl_disabled=true")
    assert db._typed_query(other) == (other, {})  # other drivers are untouched
    assert db._typed_query(make_url("mysql+mysqlconnector://u:p@h/d"))[1] == {}
