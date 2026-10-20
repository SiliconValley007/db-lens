"""Real-server tests. Skipped unless admin URLs are provided:
    DBLENS_PG_URL=postgresql+psycopg2://admin:pw@host/db  DBLENS_MYSQL_URL=mysql+pymysql://admin:pw@host/db  pytest
The account must be able to create roles/users. Oracle has no equivalent here (no server available): its SQL is unverified."""

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from rich.text import Text
from sqlalchemy import make_url, text
from sqlalchemy.exc import SQLAlchemyError

from db_lens import cli, db, store

PG, MY = os.environ.get("DBLENS_PG_URL", ""), os.environ.get("DBLENS_MYSQL_URL", "")
pytestmark = pytest.mark.live
pg = pytest.mark.skipif(not PG, reason="DBLENS_PG_URL not set")
my = pytest.mark.skipif(not MY, reason="DBLENS_MYSQL_URL not set")


@pytest.fixture(autouse=True)
def server_must_survive() -> Iterator[Any]:
    """Guard for EVERY live test: the database servers must be the same running instances afterwards. v0.7.0 shipped a teardown
    that called mysqlclient's shutdown(), i.e. mysql_shutdown(), which stops the server; uptime, the Com_shutdown counter and
    the PostgreSQL postmaster start time would all have caught it."""

    def mysql_state() -> Any:
        with db.engine(make_url(MY)) as e, e.connect() as c:
            st: dict[str, Any] = dict(c.execute(text("SHOW GLOBAL STATUS WHERE Variable_name IN ('Uptime', 'Com_shutdown')")).all())  # type: ignore[arg-type]
            return int(st["Uptime"]), int(st.get("Com_shutdown", 0))

    def pg_state() -> Any:
        with db.engine(make_url(PG)) as e, e.connect() as c:
            return c.execute(text("SELECT pg_postmaster_start_time()")).scalar()

    my0 = mysql_state() if MY else (0, 0)
    pg0 = pg_state() if PG else None
    yield
    if MY:
        up1, sd1 = mysql_state()
        assert up1 >= my0[0], f"MySQL/MariaDB restarted during the test (uptime {my0[0]}s -> {up1}s)"
        assert sd1 == my0[1], "a shutdown command reached the MySQL/MariaDB server"
    if PG:
        assert pg_state() == pg0, "PostgreSQL restarted during the test"


def sql(url: Any, *stmts: Any) -> Any:
    with db.engine(make_url(url)) as e, e.connect() as c:
        for s in stmts:
            c.execute(text(s))
        c.commit()


def with_user(url: Any, user: Any, pw: Any) -> Any:
    return make_url(url).set(username=user, password=pw)


# ------------------------------------------------------------------ PostgreSQL
@pytest.fixture
def pg_ro() -> Iterator[Any]:
    sql(
        PG,
        "DROP TABLE IF EXISTS dl_t",
        "CREATE TABLE dl_t(id int primary key, v text)",
        "INSERT INTO dl_t VALUES (1,'a')",
        "DROP ROLE IF EXISTS dl_ro",
        "CREATE ROLE dl_ro LOGIN PASSWORD 'ropw'",
        f"GRANT CONNECT ON DATABASE {make_url(PG).database} TO dl_ro",
        "GRANT USAGE ON SCHEMA public TO dl_ro",
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        "GRANT SELECT ON dl_t TO dl_ro",
    )
    yield with_user(PG, "dl_ro", "ropw")
    sql(PG, "DROP OWNED BY dl_ro", "DROP ROLE dl_ro", "DROP TABLE IF EXISTS dl_t")


@pg
def test_pg_transaction_poisoning_is_real_and_catalog_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    sql(PG, "DROP TABLE IF EXISTS dl_t", "CREATE TABLE dl_t(id int)")
    with db.engine(make_url(PG)) as e, e.connect() as c:
        with pytest.raises(SQLAlchemyError):
            c.execute(text("SELECT * FROM no_such_table"))
        with pytest.raises(SQLAlchemyError, match="aborted"):  # the failure mode the rollback guards against
            c.execute(text("SELECT 1"))
        c.rollback()
    monkeypatch.setitem(db.EST, "postgresql", ("SELECT * FROM no_such_table WHERE n = :s", *db.EST["postgresql"]))
    with db.engine(make_url(PG)) as e:
        est = db.estimates(e, "public", ["dl_t"])  # tier 1 fails and poisons; tier 2 must still work
    assert "dl_t" in est and (est["dl_t"][1] or 0) >= 0
    sql(PG, "DROP TABLE dl_t")


@pg
def test_pg_readonly_session_is_effective_and_verified(pg_ro: Any) -> None:
    with db.engine(make_url(PG)) as e, db.connect_ro(e) as c:
        assert c.execute(text("SHOW transaction_read_only")).scalar() == "on"
        for stmt in ("CREATE TABLE dl_x(a int)", "INSERT INTO dl_t VALUES (9,'z')", "DELETE FROM dl_t"):
            with pytest.raises(SQLAlchemyError, match="read-only"):
                c.execute(text(stmt))
            c.rollback()


@pg
def test_pg_server_side_cursor_leaves_no_orphan_backend() -> None:
    mon = db.make_engine(make_url(PG))

    def count() -> Any:
        with mon.connect() as c:
            return c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")).scalar()

    base = count()
    with db.engine(make_url(PG)) as e, db.connect_ro(e) as c:
        cols, rows, trunc = db.fetch_limited(c, text("SELECT g FROM generate_series(1, 50000000) g"), 5, stream=True)
        assert len(rows) == 5 and trunc
    for _ in range(50):
        if count() == base:
            break
        time.sleep(0.1)
    assert count() == base
    mon.dispose()


@pg
def test_pg_write_capability_and_strict_mode(pg_ro: Any, home: Path, runner: Any) -> None:
    with db.engine(make_url(PG)) as e:
        assert db.probe_account(e)[0] is True  # admin / superuser
    with db.engine(pg_ro) as e:
        assert db.probe_account(e)[:2] == (False, "")
    runner("connect", PG, "-n", "admin", "--plaintext", has="Write-capable account")
    assert "UDF" in " ".join(runner.last.output.split())
    runner("query", "SELECT 1 AS x", has="x")
    assert "can modify data" in runner.last.output  # per-query reminder on stderr
    runner("query", "SELECT 1", "--strict", code=1, has="not verifiably read-only")
    runner("connect", pg_ro.render_as_string(hide_password=False), "-n", "ro", "--plaintext", hasnt="Write-capable")
    runner("query", "SELECT id FROM dl_t", "-p", "ro", "--strict", "-o", "csv", has="id")
    runner("query", "SELECT * FROM dl_t", "-p", "ro", "-S", "public", has="v")


# ------------------------------------------------------------------ MySQL / MariaDB
@pytest.fixture
def my_ro() -> Iterator[Any]:
    sql(
        MY,
        "DROP USER IF EXISTS 'dl_ro'@'%'",
        "CREATE USER 'dl_ro'@'%' IDENTIFIED BY 'ropw'",
        "DROP TABLE IF EXISTS dl_t",
        "CREATE TABLE dl_t(id int primary key, v text)",
        "INSERT INTO dl_t VALUES (1,'a')",
        f"GRANT SELECT ON {make_url(MY).database}.* TO 'dl_ro'@'%'",
    )
    yield with_user(MY, "dl_ro", "ropw")
    sql(MY, "DROP USER IF EXISTS 'dl_ro'@'%'", "DROP TABLE IF EXISTS dl_t")


@my
def test_mysql_readonly_session_is_effective_and_verified(my_ro: Any) -> None:
    with db.engine(make_url(MY)) as e, db.connect_ro(e) as c:  # even for an account with full privileges
        for stmt in ("CREATE TABLE dl_x(a int)", "INSERT INTO dl_t VALUES (9,'z')", "DELETE FROM dl_t"):
            with pytest.raises(SQLAlchemyError, match="READ ONLY"):
                c.execute(text(stmt))


@my
def test_mysql_write_capability(my_ro: Any) -> None:
    with db.engine(make_url(MY)) as e:
        assert db.probe_account(e)[0] is True
    with db.engine(my_ro) as e:
        assert db.probe_account(e)[:2] == (False, "")


@pytest.fixture
def my_big() -> Iterator[Any]:
    sql(
        MY,
        "DROP TABLE IF EXISTS dl_big",
        "CREATE TABLE dl_big(id int primary key auto_increment, v varchar(100))",
        "INSERT INTO dl_big(v) SELECT repeat('x',100) FROM information_schema.columns a, information_schema.columns b LIMIT 400000",
    )
    yield
    sql(MY, "DROP TABLE IF EXISTS dl_big")


@my
def test_mysql_early_stop_is_fast_and_kills_the_server_query(my_big: Any) -> None:
    with db.engine(make_url(MY)) as e:
        t = time.perf_counter()  # baseline: the old behaviour (cursor.close() drains)
        with db.connect_ro(e) as c:
            r = c.execute(text("SELECT * FROM dl_big"), execution_options={"stream_results": True})
            r.fetchmany(11)
            r.close()
        drain = time.perf_counter() - t
        t = time.perf_counter()
        with db.connect_ro(e) as c:
            _, rows, trunc = db.fetch_limited(c, text("SELECT * FROM dl_big"), 10, stream=True)
        abort = time.perf_counter() - t
    assert len(rows) == 10 and trunc
    assert abort < drain / 3 and abort < 1.0, (abort, drain)
    with db.engine(make_url(MY)) as e, e.connect() as c:
        for _ in range(50):  # the statement must not keep running server-side
            live = c.execute(text("SELECT count(*) FROM information_schema.processlist WHERE info LIKE 'SELECT * FROM dl_big%'")).scalar()
            if live == 0:
                break
            time.sleep(0.1)
        assert live == 0


@my
def test_mysql_cli_limit_on_huge_table_is_fast_and_connection_not_reused(my_big: Any, home: Path, runner: Any) -> None:
    runner("connect", MY, "-n", "m", "--plaintext")
    t = time.perf_counter()
    runner("query", "SELECT * FROM dl_big", "-l", "5", "-o", "csv", "--timeout", "30", has="id")
    assert time.perf_counter() - t < 2.0
    assert cli.csv_safe("=x") == "'=x"


# ------------------------------------------------------------------ every MySQL driver, real server
def _streams(e: Any) -> bool:
    """Does this SQLAlchemy/driver pair really stream? (mysql-connector with SQLAlchemy 2.0.x cannot.)"""
    with db.connect_ro(e) as c:
        db.fetch_limited(c, text("SELECT 1"), 5, stream=True)
    return bool(e.dialect.supports_server_side_cursors)


def _driver_url(driver: Any) -> Any:
    pytest.importorskip({"pymysql": "pymysql", "mysqldb": "MySQLdb", "mysqlconnector": "mysql.connector"}[driver])
    return make_url(MY).set(drivername=f"mysql+{driver}")


@my
@pytest.mark.parametrize("driver", ["pymysql", "mysqldb", "mysqlconnector"])
def test_mysql_drivers_early_stop_is_fast_streams_and_kills_the_query(my_big: Any, driver: Any) -> None:
    url = _driver_url(driver)
    with db.engine(url) as e:
        if not _streams(e):
            pytest.skip(f"{driver}: this SQLAlchemy cannot stream (results are buffered client-side)")
        t = time.perf_counter()
        with db.connect_ro(e) as c:  # baseline: what closing the cursor normally costs
            r = c.execute(text("SELECT * FROM dl_big"), execution_options={"stream_results": True})
            first = time.perf_counter() - t  # time to first rows: proves the result is not buffered
            r.fetchmany(11)
            r.close()
        drain = time.perf_counter() - t
        t = time.perf_counter()
        with db.connect_ro(e) as c:
            cols, rows, trunc = db.fetch_limited(c, text("SELECT * FROM dl_big"), 10, stream=True)
            assert c.invalidated
        abort = time.perf_counter() - t
    assert len(rows) == 10 and trunc and rows[0][0] == 1
    assert first < 1.0, f"{driver} buffered the whole result ({first:.1f}s)"
    assert abort < max(drain / 3, 0.25) and abort < 1.0, (driver, abort, drain)
    with db.engine(make_url(MY)) as e, e.connect() as c:
        for _ in range(50):
            live = c.execute(text("SELECT count(*) FROM information_schema.processlist WHERE info LIKE 'SELECT * FROM dl_big%'")).scalar()
            if live == 0:
                break
            time.sleep(0.1)
        assert live == 0, "server kept running the aborted statement"


@my
@pytest.mark.parametrize("driver", ["pymysql", "mysqldb", "mysqlconnector"])
def test_mysql_drivers_log_nothing_and_fresh_connections_still_work_after_abort(my_big: Any, driver: Any, caplog: pytest.LogCaptureFixture) -> None:
    import gc
    import sys

    unraisable: list[Any] = []
    old, sys.unraisablehook = sys.unraisablehook, unraisable.append  # 'Exception ignored in ...__del__' goes here, not to logging
    try:
        with db.engine(_driver_url(driver)) as e:

            def uptime() -> int:
                return int(e.connect().execute(text("SHOW GLOBAL STATUS LIKE 'Uptime'")).one()[1])

            up0 = uptime()
            if not _streams(e):
                pytest.skip(f"{driver}: this SQLAlchemy cannot stream")
            reports: list[Any] = []
            with db.connect_ro(e) as c:
                db.fetch_limited(c, text("SELECT * FROM dl_big"), 5, stream=True, on_discard=reports.append)
            with db.connect_ro(e) as c:
                assert db.fetch_limited(c, text("SELECT count(*) FROM dl_big"), 5)[1][0][0] > 5
        gc.collect()
    finally:
        sys.unraisablehook = old
    assert reports[0].method == "driver", reports
    with db.engine(make_url(MY)) as e, e.connect() as c:  # the SERVER must still be the same running instance
        assert int(c.execute(text("SHOW GLOBAL STATUS LIKE 'Uptime'")).one()[1]) >= up0
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")]  # no 'Error closing cursor' noise
    assert not unraisable, [str(u.exc_value) for u in unraisable]  # v0.6.0 leaked MySQLResult.__del__ errors here


@my
@pytest.mark.parametrize("driver", ["pymysql", "mysqldb", "mysqlconnector"])
def test_mysql_driver_internals_gone_still_fast_and_exception_free(my_big: Any, driver: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import gc
    import sys

    monkeypatch.setattr(db, "TIDY", {})  # simulate a driver release that renamed its private state
    reports: list[Any] = []
    unraisable: list[Any] = []
    old, sys.unraisablehook = sys.unraisablehook, unraisable.append
    try:
        with db.engine(_driver_url(driver)) as e, db.connect_ro(e) as c:
            if not _streams(e):
                pytest.skip(f"{driver}: this SQLAlchemy cannot stream")
            t = time.perf_counter()
            _, rows, trunc = db.fetch_limited(c, text("SELECT * FROM dl_big"), 10, stream=True, on_discard=reports.append)
        elapsed = time.perf_counter() - t
        gc.collect()
    finally:
        sys.unraisablehook = old
    assert len(rows) == 10 and trunc and reports[0].method == "generic" and elapsed < 1.0
    # the documented cost of losing TIDY: at most a GC-time 'Exception ignored' from PyMySQL, never an error to the caller
    assert all("MySQLResult" in str(u.object) or "SSCursor" in str(u.object) for u in unraisable), [str(u.object) for u in unraisable]
    assert driver == "pymysql" or not unraisable


# ------------------------------------------------------------------ privilege probing on real servers
@pg
def test_pg_probe_sees_noinherit_role_reachable_by_set_role_and_routines() -> None:
    sql(
        PG,
        "DROP TABLE IF EXISTS dl_t",
        "CREATE TABLE dl_t(id int)",
        "DROP ROLE IF EXISTS dl_hop",
        "DROP ROLE IF EXISTS dl_writer",
        "DROP ROLE IF EXISTS dl_plain",
        "CREATE ROLE dl_writer NOLOGIN",
        "GRANT INSERT ON dl_t TO dl_writer",
        "CREATE ROLE dl_hop LOGIN PASSWORD 'x' NOINHERIT",
        "GRANT dl_writer TO dl_hop",
        "CREATE ROLE dl_plain LOGIN PASSWORD 'x'",
        "GRANT USAGE ON SCHEMA public TO dl_hop, dl_plain",
        "GRANT SELECT ON dl_t TO dl_hop, dl_plain",
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        "CREATE OR REPLACE FUNCTION dl_fn() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
    )
    try:
        with db.engine(with_user(PG, "dl_hop", "x")) as e:
            writable, why, risks = db.probe_account(e)
            assert writable is True and "SET ROLE" in why  # privileges NOT inherited, yet one SET ROLE away
            with e.connect() as c:  # the plain table-privilege check misses it
                assert c.execute(text("SELECT has_table_privilege('dl_t', 'INSERT')")).scalar() is False
            assert any(r.kind == "custom-routine" and "1 with" in r.message for r in risks)
        with db.engine(with_user(PG, "dl_plain", "x")) as e:
            writable, _, risks = db.probe_account(e)
            assert writable is False and any(r.kind == "custom-routine" and r.names == ("dl_fn",) for r in risks)  # PUBLIC may EXECUTE by default
    finally:
        sql(
            PG,
            "DROP FUNCTION IF EXISTS dl_fn()",
            "DROP OWNED BY dl_hop",
            "DROP OWNED BY dl_plain",
            "DROP OWNED BY dl_writer",
            "DROP ROLE dl_hop",
            "DROP ROLE dl_plain",
            "DROP ROLE dl_writer",
            "DROP TABLE IF EXISTS dl_t",
        )


@my
def test_mysql_probe_resolves_non_default_roles_and_reports_visible_routines() -> None:
    db_name = make_url(MY).database
    sql(
        MY,
        "DROP TABLE IF EXISTS dl_t",
        "CREATE TABLE dl_t(id int)",
        "DROP USER IF EXISTS 'dl_member'@'%'",
        "DROP USER IF EXISTS 'dl_reader'@'%'",
        "DROP ROLE IF EXISTS dl_writer_role",
        "DROP FUNCTION IF EXISTS dl_fn",
        "CREATE ROLE dl_writer_role",
        f"GRANT INSERT ON {db_name}.dl_t TO dl_writer_role",
        "CREATE USER 'dl_member'@'%' IDENTIFIED BY 'x'",
        "GRANT dl_writer_role TO 'dl_member'@'%'",
        f"GRANT SELECT ON {db_name}.* TO 'dl_member'@'%'",
        "CREATE USER 'dl_reader'@'%' IDENTIFIED BY 'x'",
        f"GRANT SELECT ON {db_name}.* TO 'dl_reader'@'%'",
        "CREATE FUNCTION dl_fn() RETURNS int DETERMINISTIC RETURN 1",
        f"GRANT EXECUTE ON FUNCTION {db_name}.dl_fn TO 'dl_member'@'%'",
    )
    try:
        with db.engine(with_user(MY, "dl_member", "x")) as e:
            writable, why, risks = db.probe_account(e)
            # a plain account may not SHOW GRANTS for a role (needs SELECT on mysql.*): that must surface, never read as "safe"
            assert writable is False and any("dl_writer_role" in r.message and "could not be resolved statically" in r.message for r in risks)
            assert any(r.kind == "custom-routine" and r.names == ("dl_fn",) for r in risks)
        sql(MY, "GRANT SELECT ON mysql.* TO 'dl_member'@'%'")
        with db.engine(with_user(MY, "dl_member", "x")) as e:
            writable, why, risks = db.probe_account(e)
            assert writable is True and "dl_writer_role grants INSERT" in why, (writable, why, risks)  # resolved through the role
        with db.engine(with_user(MY, "dl_reader", "x")) as e:
            assert db.probe_account(e) == (False, "", [])
    finally:
        sql(
            MY,
            "DROP USER IF EXISTS 'dl_member'@'%'",
            "DROP USER IF EXISTS 'dl_reader'@'%'",
            "DROP ROLE IF EXISTS dl_writer_role",
            "DROP FUNCTION IF EXISTS dl_fn",
            "DROP TABLE IF EXISTS dl_t",
        )


@pg
def test_pg_extension_functions_are_informational_custom_ones_are_named() -> None:
    sql(
        PG,
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "DROP ROLE IF EXISTS dl_plain",
        "CREATE ROLE dl_plain LOGIN PASSWORD 'x'",
        "GRANT USAGE ON SCHEMA public TO dl_plain",
        "CREATE OR REPLACE FUNCTION dl_fn() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    )
    try:
        with db.engine(with_user(PG, "dl_plain", "x")) as e:
            writable, _, risks = db.probe_account(e)
        custom = [r for r in risks if r.kind == "custom-routine"]
        ext = [r for r in risks if r.kind == "extension-routine"]
        assert writable is False
        assert len(custom) == 1 and custom[0].names == ("dl_fn",) and custom[0].blocking and "1 user-created routine(s)" in custom[0].message
        assert len(ext) == 1 and not ext[0].blocking  # pg_trgm's similarity() etc.: standard code, informational
        assert "pg_catalog" not in custom[0].message and "similarity" not in custom[0].message
        panel = cli.risk_panel(writable, "", risks)
        assert panel is not None and cast(Text, panel.renderable).plain.count("Standard / extension functions") == 1
    finally:
        sql(PG, "DROP FUNCTION IF EXISTS dl_fn()", "DROP OWNED BY dl_plain", "DROP ROLE dl_plain", "DROP EXTENSION IF EXISTS pg_trgm")


@pg
def test_pg_legacy_routine_query_is_valid_on_this_server() -> None:
    with db.engine(make_url(PG)) as e, e.connect() as c:
        assert isinstance(c.execute(text(db.PG_ROUTINES_LEGACY.replace("NOT p.proisagg AND NOT p.proiswindow", "true"))).all(), list)


@pg
def test_pg_error_codes_are_normalised_from_psycopg2() -> None:
    with db.engine(make_url(PG)) as e, e.connect() as c:
        try:
            c.execute(text("SELECT * FROM no_such_table"))
        except SQLAlchemyError as err:
            assert db.error_code(err) == "SQLSTATE-42P01"


@my
def test_mysql_error_codes_are_normalised_per_driver() -> None:
    for driver in ("pymysql", "mysqldb", "mysqlconnector"):
        with db.engine(_driver_url(driver)) as e, e.connect() as c:
            try:
                c.execute(text("SELECT * FROM no_such_table"))
            except SQLAlchemyError as err:
                assert db.error_code(err) == "MY-1146", (driver, err)


@my
@pytest.mark.parametrize("driver", ["pymysql", "mysqldb", "mysqlconnector"])
def test_mysql_still_correct_when_streaming_is_unavailable(my_big: Any, driver: Any) -> None:
    """SQLAlchemy 2.0.x + mysql-connector (and any future driver that cannot stream): results are buffered, but queries,
    --limit and the discard path must still work and never raise."""
    with db.engine(_driver_url(driver)) as e, db.connect_ro(e) as c:
        e.dialect.supports_server_side_cursors = False  # what the fallback leaves behind
        _, rows, trunc = db.fetch_limited(c, text("SELECT id FROM dl_big ORDER BY id"), 7, stream=True)
    assert [r[0] for r in rows] == list(range(1, 8)) and trunc


# ------------------------------------------------------------------ teardown must never send server-control commands
@my
@pytest.mark.parametrize("driver", ["pymysql", "mysqldb", "mysqlconnector"])
def test_teardown_steps_per_driver_and_no_server_control_call(my_big: Any, driver: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _driver_url(driver)  # importorskip: skip (not fail) when the driver is not installed
    calls: list[Any] = []
    if driver == "mysqldb":
        import MySQLdb.connections as mc

        monkeypatch.setattr(mc.Connection, "shutdown", lambda self, *a: calls.append("mysql_shutdown() was called"))
    reports: list[Any] = []
    with db.engine(_driver_url(driver)) as e, db.connect_ro(e) as c:
        module = type(c.connection.driver_connection).__module__
        _, rows, trunc = db.fetch_limited(c, text("SELECT * FROM dl_big"), 10, stream=True, on_discard=reports.append)
    assert len(rows) == 10 and trunc and calls == []
    steps = set(reports[0].steps)
    expected = (
        {"mysql.connector.connection": "shutdown", "mysql.connector.connection_cext": "cext-close"}.get(module)
        if driver == "mysqlconnector"
        else None
    )
    assert steps & {"shutdown", "cext-close"} == ({expected} if expected else set()), (driver, module, steps)
    assert "neuter-close" in steps and reports[0].method == "driver"


@my
@pytest.mark.parametrize("driver", ["pymysql", "mysqldb", "mysqlconnector"])
def test_mysql_connector_and_all_drivers_stream_on_every_sqlalchemy(my_big: Any, driver: Any) -> None:
    """No driver may silently fall back to buffering: the unbuffered cursor must really be used."""
    with db.engine(_driver_url(driver)) as e:
        assert e.dialect.supports_server_side_cursors is True
        with db.connect_ro(e) as c:
            t = time.perf_counter()
            r = c.execute(text("SELECT * FROM dl_big"), execution_options={"stream_results": True})
            assert time.perf_counter() - t < 1.0, f"{driver} buffered the whole result"
            r.fetchmany(3)
            db.hard_close(c, r)


# ------------------------------------------------------------------ statement timeouts on real servers
@pg
def test_pg_statement_timeout_really_aborts_the_query() -> None:
    with db.engine(make_url(PG)) as e, db.connect_ro(e, timeout=0.5) as c:
        t = time.perf_counter()
        with pytest.raises(SQLAlchemyError) as err:
            c.execute(text("SELECT pg_sleep(5)"))
    assert time.perf_counter() - t < 3 and db.error_code(err.value) == "SQLSTATE-57014"


@my
@pytest.mark.parametrize("scheme", ["mysql+pymysql", "mariadb+pymysql"])
def test_mariadb_statement_timeout_works_behind_mysql_and_mariadb_urls(scheme: Any) -> None:
    """v0.7.0 used MySQL's max_execution_time for MariaDB reached through mysql://: it does not exist there, so --timeout was
    silently ignored."""
    url = make_url(MY).set(drivername=scheme)
    with db.engine(url) as e, db.connect_ro(e, timeout=0.5) as c:
        assert not c.info.get("warnings"), c.info.get("warnings")
        t = time.perf_counter()
        with pytest.raises(SQLAlchemyError) as err:
            c.execute(text("SELECT SLEEP(5)")).all()
    assert time.perf_counter() - t < 3, "statement timeout was ignored"
    assert db.error_code(err.value) == "MY-1969"  # "Query execution was interrupted (max_statement_time exceeded)"


# ------------------------------------------------------------------ TLS: mapped per driver, then verified on the live connection
MY_CA = os.environ.get("DBLENS_MYSQL_TLS_CA")  # CA that signed the MySQL/MariaDB server certificate (enables verify-* tests)
BAD_CA = os.environ.get("DBLENS_BAD_CA", "/etc/ssl/certs/ssl-cert-snakeoil.pem")


def _server_has_tls(url_str: str, query: str) -> bool:
    with db.engine(make_url(url_str)) as e, e.connect() as c:
        row = c.execute(text(query)).first()
    return row is not None and str(row[-1]).lower() in {"yes", "on", "true", "1"}


@pg
def test_pg_tls_modes_are_requested_and_verified() -> None:
    from db_lens import conn

    if not _server_has_tls(PG, "SHOW ssl"):
        pytest.skip("PostgreSQL server has ssl = off")
    for mode, expected in (("require", True), ("disable", False)):
        url = conn.build_url(uri=PG, ssl_mode=mode)
        with db.engine(url) as e:
            assert db.tls_in_use(e) is expected, mode
    with db.engine(conn.build_url(uri=PG, ssl_mode="disable")) as e, pytest.raises(db.TlsError, match="NOT encrypted"):
        db.assert_tls(e, "require")
    with db.engine(conn.build_url(uri=PG, ssl_mode="require")) as e:
        db.assert_tls(e, "require")  # passes


@pg
def test_pg_cli_connect_with_ssl_stores_and_enforces_the_demand(home: Path, runner: Any) -> None:
    if not _server_has_tls(PG, "SHOW ssl"):
        pytest.skip("PostgreSQL server has ssl = off")
    u = make_url(PG)
    runner(
        "connect",
        "-D",
        "postgresql",
        "--host",
        u.host,
        "--port",
        str(u.port or 5432),
        "-u",
        u.username,
        "-d",
        u.database,
        "--password-env",
        "DBL_PGPW",
        "--ssl-mode",
        "require",
        "-n",
        "pgtls",
        "--plaintext",
        code=1,
        has="DBL_PGPW is not set",
    )
    os.environ["DBL_PGPW"] = u.password or ""
    try:
        runner(
            "connect",
            "-D",
            "postgresql",
            "--host",
            u.host,
            "--port",
            str(u.port or 5432),
            "-u",
            u.username,
            "-d",
            u.database,
            "--password-env",
            "DBL_PGPW",
            "--ssl-mode",
            "require",
            "-n",
            "pgtls",
            has="TLS: require (verified)",
        )
        assert store.load()["profiles"]["pgtls"]["tls"] == "require" and "sslmode=require" in store.load()["profiles"]["pgtls"]["uri"]
        runner("query", "SELECT 1 AS x", "-p", "pgtls", has="x")
        assert "DBL_PGPW" in store.load()["profiles"]["pgtls"]["uri"] and (u.password or "x") not in store.CONFIG.read_text()
    finally:
        del os.environ["DBL_PGPW"]


def _mysql_tls_url(driver: str, mode: str, ca: str | None = None) -> object:
    from db_lens import conn

    _driver_url(driver)  # skip when the driver is not installed
    return conn.build_url(uri=str(make_url(MY).set(drivername=f"mysql+{driver}").render_as_string(hide_password=False)), ssl_mode=mode, ssl_ca=ca)


@my
@pytest.mark.parametrize("driver", ["pymysql", "mysqldb", "mysqlconnector"])
@pytest.mark.parametrize("mode", ["require", "disable"])
def test_mysql_tls_require_and_disable_per_driver(driver: Any, mode: Any) -> None:
    if not _server_has_tls(MY, "SHOW VARIABLES LIKE 'have_ssl'"):
        pytest.skip("MySQL/MariaDB server has no TLS configured")
    with db.engine(_mysql_tls_url(driver, mode)) as e:  # type: ignore[arg-type]
        assert db.tls_in_use(e) is (mode == "require"), (driver, mode)


@my
@pytest.mark.parametrize("driver", ["pymysql", "mysqldb", "mysqlconnector"])
@pytest.mark.parametrize("mode", ["verify-ca", "verify-full"])
def test_mysql_tls_verify_modes_accept_the_right_ca_and_reject_the_wrong_one(driver: Any, mode: Any) -> None:
    if not MY_CA:
        pytest.skip("DBLENS_MYSQL_TLS_CA not set")
    with db.engine(_mysql_tls_url(driver, mode, MY_CA)) as e:  # type: ignore[arg-type]
        assert db.tls_in_use(e) is True
        db.assert_tls(e, mode)
    with db.engine(_mysql_tls_url(driver, mode, BAD_CA)) as e, pytest.raises(SQLAlchemyError):  # type: ignore[arg-type]
        db.tls_in_use(e)  # certificate not signed by this CA: connection refused


@my
def test_a_driver_that_does_not_encrypt_by_default_is_caught_by_the_post_connect_check(home: Path, runner: Any) -> None:
    """mysqlclient never negotiates TLS on its own: a profile demanding it must refuse to run, not silently go plaintext."""
    if not _server_has_tls(MY, "SHOW VARIABLES LIKE 'have_ssl'"):
        pytest.skip("MySQL/MariaDB server has no TLS configured")
    _driver_url("mysqldb")
    plain = str(make_url(MY).set(drivername="mysql+mysqldb").render_as_string(hide_password=False))
    runner("connect", plain, "-n", "plain", "--plaintext", has="Connected")
    cfg = store.load()
    cfg["profiles"]["plain"]["tls"] = "require"
    store.save(cfg)
    runner("tables", code=1, has="TlsError")
    assert "NOT encrypted" in " ".join(runner.last.output.split()) or "encrypted" in runner.last.output


# ------------------------------------------------------------------ SSH tunnel through a real sshd (system OpenSSH client)
SSH_HOST, SSH_KEY, SSH_LOG = os.environ.get("DBLENS_SSH_HOST"), os.environ.get("DBLENS_SSH_KEY"), os.environ.get("DBLENS_SSH_LOG")
SSH_UNTRUSTED = os.environ.get("DBLENS_SSH_HOST_UNTRUSTED")
ssh = pytest.mark.skipif(not (SSH_HOST and SSH_KEY), reason="DBLENS_SSH_HOST / DBLENS_SSH_KEY not set")


def _forwarded(port: int) -> int:
    """How many connections sshd forwarded to 127.0.0.1:<port> so far (needs LogLevel DEBUG1 and DBLENS_SSH_LOG)."""
    if not SSH_LOG or not os.path.exists(SSH_LOG):
        return -1
    with open(SSH_LOG, encoding="utf-8", errors="replace") as f:
        return sum(f"connected to 127.0.0.1 port {port}" in line for line in f)


def _tunnel_processes(local_port: int) -> int:
    import subprocess

    r = subprocess.run(["pgrep", "-f", f"127.0.0.1:{local_port}:"], capture_output=True, text=True)
    return len(r.stdout.split())


@ssh
@pytest.mark.parametrize("which", ["pg", "my"])
def test_ssh_tunnel_reaches_the_database_and_cleans_up(which: Any, tmp_path: Path) -> None:
    from db_lens import tunnel

    url = make_url(PG if which == "pg" else MY)
    if (which == "pg" and not PG) or (which == "my" and not MY):
        pytest.skip("server URL not set")
    port = url.port or (5432 if which == "pg" else 3306)
    before = _forwarded(port)
    spec = tunnel.parse_target(SSH_HOST or "", key=SSH_KEY, accept_new=True)
    with tunnel.forward(url, spec) as routed:
        assert routed.port is not None
        local = routed.port
        assert local != url.port and routed.host in ("127.0.0.1", url.host)
        if which == "pg":
            assert routed.query["hostaddr"] == "127.0.0.1"
        with db.engine(routed) as e, e.connect() as c:
            assert c.execute(text("SELECT 1")).scalar() == 1
        if sys_platform_posix():
            assert _tunnel_processes(local) == 1  # the ssh child is alive while the block runs
    if sys_platform_posix():
        assert _tunnel_processes(local) == 0  # ... and gone afterwards: no orphaned tunnel
    if before >= 0:
        assert _forwarded(port) > before  # sshd really forwarded the traffic


def sys_platform_posix() -> bool:
    import shutil
    import sys

    return sys.platform != "win32" and shutil.which("pgrep") is not None


@ssh
def test_ssh_cli_end_to_end_with_profile_commands(home: Path, runner: Any) -> None:
    pgu = make_url(PG) if PG else None
    if pgu is None:
        pytest.skip("DBLENS_PG_URL not set")
    runner(
        "connect",
        "-D",
        "postgresql",
        "--host",
        pgu.host,
        "--port",
        str(pgu.port or 5432),
        "-u",
        pgu.username,
        "-d",
        pgu.database,
        "--password-env",
        "DBL_SSHPW",
        "--ssh",
        SSH_HOST,
        "--ssh-key",
        SSH_KEY,
        "--ssh-accept-new",
        "-n",
        "viassh",
        "--plaintext",
        code=1,
        has="DBL_SSHPW is not set",
    )
    os.environ["DBL_SSHPW"] = pgu.password or ""
    try:
        runner(
            "connect",
            "-D",
            "postgresql",
            "--host",
            pgu.host,
            "--port",
            str(pgu.port or 5432),
            "-u",
            pgu.username,
            "-d",
            pgu.database,
            "--password-env",
            "DBL_SSHPW",
            "--ssh",
            SSH_HOST,
            "--ssh-key",
            SSH_KEY,
            "--ssh-accept-new",
            "-n",
            "viassh",
            has=["Connected viassh", "via ssh"],
        )
        out = runner("tables", "-p", "viassh")
        assert "table(s)" in runner.last.output or "No tables found" in runner.last.output, out
        assert json.loads(runner("profiles", "-o", "json"))[0]["ssh"].startswith("tunnel@")
        runner("query", "SELECT inet_server_port() AS p", "-p", "viassh", has="p")
        runner("profiles", "rename", "viassh", "tunneled", has="Renamed")
        runner("tables", "-p", "tunneled")
        assert "table(s)" in runner.last.output or "No tables found" in runner.last.output
        runner("profiles", "remove", "tunneled", "-y", has="Removed")
    finally:
        del os.environ["DBL_SSHPW"]


@ssh
def test_ssh_failures_are_clean_panels(home: Path, runner: Any, tmp_path: Path) -> None:
    base = ["connect", "-D", "mysql", "--host", "127.0.0.1", "-u", "x", "-d", "y", "-n", "bad"]
    runner(*base, "--ssh", SSH_HOST, "--ssh-key", str(tmp_path / "missing"), code=1, has=["TunnelError", "key file not found"])
    wrong = tmp_path / "wrongkey"
    import subprocess

    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(wrong)], check=True)
    runner(*base, "--ssh", SSH_HOST, "--ssh-key", str(wrong), "--ssh-accept-new", code=1, has=["TunnelError", "rejected the key"])
    runner(*base, "--ssh", "tunnel@127.0.0.1:1", "--ssh-key", SSH_KEY, "--ssh-accept-new", code=1, has=["TunnelError", "Nothing is listening"])
    if SSH_UNTRUSTED:  # a server whose host key was never trusted: refuse, do not prompt
        runner(*base, "--ssh", SSH_UNTRUSTED, "--ssh-key", SSH_KEY, code=1, has=["TunnelError", "--ssh-accept-new"])
    assert store.load()["profiles"] == {}
