"""Offline (mock) equivalents of every live test in tests/test_integration.py. Each live test keeps its `live` marker and runs in CI
against real servers; these prove the same decision logic (statements, ordering, fail-closed paths, cleanup) without a server.
EQUIVALENTS maps live test -> offline test and is checked for completeness by test_every_live_test_has_an_offline_equivalent."""

from __future__ import annotations

import ast
import json
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import typer
from sqlalchemy import make_url
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.dialects.mysql import mysqlconnector
from sqlalchemy.exc import OperationalError, SQLAlchemyError, StatementError

from db_lens import cli, db, sqlguard, store, tunnel
from db_lens import conn as dbconn

from .fakes import FakeConn, FakeEngine, FakeResult, as_real, fc, new_dialect
from .test_mysql_drivers import NoInternals, Raw, lookalike, noop_closed
from .test_mysql_drivers import conn as my_conn
from .test_tunnel import FakeProc, fake_ssh  # noqa: F401  (fixture)

PG, MY = new_dialect(postgresql), new_dialect(mysql)
DRIVERS = ["pymysql", "mysqldb", "mysqlconnector"]


def pg_engine(script: Any = ()) -> FakeEngine:
    return FakeEngine(FakeConn(PG, script))


def patch_engine(monkeypatch: pytest.MonkeyPatch, eng: Any) -> None:
    @contextmanager
    def fake(u: Any) -> Iterator[Any]:
        yield as_real(eng)

    monkeypatch.setattr(db, "engine", fake)


# ---------------------------------------------------------------------------------------------------------- PostgreSQL
def test_offline_pg_poisoning_and_catalog_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    class Poison(FakeConn):
        poisoned = False

        def execute(self, q: Any, params: Any = None, **kw: Any) -> FakeResult:
            self.log.append((str(q), params))
            if self.poisoned:
                raise SQLAlchemyError("current transaction is aborted, commands ignored")
            if "no_such_table" in str(q):
                self.poisoned = True
                raise SQLAlchemyError('relation "no_such_table" does not exist')
            return FakeResult([("dl_t", 5, 8192)])

        def rollback(self) -> None:
            self.poisoned = False
            super().rollback()

    c = Poison(PG)
    with pytest.raises(SQLAlchemyError):
        c.execute("SELECT * FROM no_such_table")
    with pytest.raises(SQLAlchemyError, match="aborted"):
        c.execute("SELECT 1")
    c.rollback()
    monkeypatch.setitem(db.EST, "postgresql", ("SELECT * FROM no_such_table WHERE n = :s", *db.EST["postgresql"]))
    c2 = Poison(PG)
    assert db.estimates(as_real(FakeEngine(c2)), "public", ["dl_t"]) == {"dl_t": (5, 8192)} and c2.rollbacks == 1


def test_offline_pg_readonly_session_verified_and_writes_refused() -> None:
    c = FakeConn(PG, [("SHOW transaction_read_only", [("on",)])])
    with db.connect_ro(as_real(FakeEngine(c))) as cc:
        assert fc(cc).options == [{"postgresql_readonly": True}] and fc(cc).sql == ["SHOW transaction_read_only"]
    with pytest.raises(db.SessionError, match="not in effect"), db.connect_ro(as_real(pg_engine([("SHOW transaction_read_only", [("off",)])]))):
        pytest.fail("must refuse")
    for stmt in ("CREATE TABLE dl_x(a int)", "INSERT INTO dl_t VALUES (9,'z')", "DELETE FROM dl_t"):
        with pytest.raises(sqlguard.UnsafeSQL):
            sqlguard.validate(stmt, "postgresql")


def test_offline_pg_server_side_cursor_is_closed_and_never_drained() -> None:
    res = FakeResult([(i,) for i in range(50)], ["g"])
    c = FakeConn(PG)

    def execute(q: Any, params: Any = None, **kw: Any) -> Any:
        c.exec_kw.append(kw)
        return res

    c.execute = execute  # type: ignore[method-assign]
    cols, rows, trunc = db.fetch_limited(as_real(c), "q", 5, stream=True)
    assert (cols, len(rows), trunc) == (["g"], 5, True) and res.closed and res.fetched == 6
    assert c.exec_kw == [{"execution_options": {"stream_results": True}}] and not c.invalidated


def test_offline_pg_write_capability_and_strict_mode(prof: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = pg_engine([("pg_roles r", [(True,)]), ("pg_depend", [])])
    ok, why, risks = db.probe_account(as_real(eng))
    assert ok is True and "SET ROLE" in why and risks == []
    assert db.probe_account(as_real(pg_engine([("pg_roles r", [(False,)]), ("pg_depend", [])])))[0] is False
    monkeypatch.setattr(db, "probe_account", lambda e: (True, "grants include ALL", []))
    runner("query", "SELECT 1", "--strict", code=1, has="not verifiably read-only")


def test_offline_pg_probe_noinherit_roles_and_routines() -> None:
    assert "pg_has_role(current_user, r.oid, 'MEMBER')" in db.PG_WRITE and "rolsuper" in db.PG_WRITE
    rows = [(False, True, 2, ["public.f1", "public.f2"], ["f1", "f2"])]
    ok, _, risks = db.probe_account(as_real(pg_engine([("pg_roles r", [(True,)]), ("pg_depend", rows)])))
    assert ok is True and [r.kind for r in risks] == ["custom-routine"] and risks[0].names == ("f1", "f2") and risks[0].blocking


def test_offline_pg_extension_functions_informational_custom_named() -> None:
    rows = [(True, False, 3, ["public.similarity"], ["similarity"]), (False, True, 1, ["public.myfn"], ["myfn"])]
    _, _, risks = db.probe_account(as_real(pg_engine([("pg_roles r", [(False,)]), ("pg_depend", rows)])))
    ext, custom = [r for r in risks if r.kind == "extension-routine"], [r for r in risks if r.kind == "custom-routine"]
    assert len(ext) == 1 and not ext[0].blocking and custom[0].names == ("myfn",) and "similarity" not in custom[0].message
    panel = cli.risk_panel(False, "", risks)
    assert panel is not None and str(panel.renderable).count("Standard / extension functions") <= 1


def test_offline_pg_legacy_routine_query_fallback() -> None:
    c = FakeConn(PG, [("pg_roles r", [(False,)]), ("p.prokind IN", SQLAlchemyError("column p.prokind does not exist")), ("p.proisagg", [])])
    assert db.probe_account(as_real(FakeEngine(c)))[0] is False
    assert c.rollbacks == 1 and "p.proisagg" in c.sql[-1] and "prokind" not in c.sql[-1]


def test_offline_pg_error_codes_normalised() -> None:
    class UniqueViolation(Exception):
        __module__ = "psycopg2.errors"
        pgcode = "23505"

    wrapped = SQLAlchemyError("dup")
    wrapped.orig = UniqueViolation("duplicate")  # type: ignore[attr-defined]
    assert db.error_code(wrapped) == "SQLSTATE-23505"


def test_offline_pg_statement_timeout_set_and_reported(capsys: pytest.CaptureFixture[str]) -> None:
    c = FakeConn(PG, [("SHOW transaction_read_only", [("on",)])])
    with db.connect_ro(as_real(FakeEngine(c)), timeout=2.5) as cc:
        assert "SET statement_timeout = 2500" in fc(cc).sql
    with pytest.raises(typer.Exit), cli.guard():
        raise OperationalError("SELECT pg_sleep(9)", {}, Exception("canceling statement due to statement timeout"))
    assert "Timed out" in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------------------- MySQL/MariaDB
def test_offline_mysql_readonly_session_verified() -> None:
    ok = FakeConn(MY, [("@@session.transaction_read_only", [(1,)])])
    with db.connect_ro(as_real(FakeEngine(ok))) as c:
        assert fc(c).sql == ["SET SESSION TRANSACTION READ ONLY", "SELECT @@session.transaction_read_only"]
    bad = FakeConn(MY, [("@@session.transaction_read_only", [(0,)])])
    with pytest.raises(db.SessionError, match="not in effect"), db.connect_ro(as_real(FakeEngine(bad))):
        pytest.fail("must refuse")


def test_offline_mysql_write_capability_and_roles_and_routines() -> None:
    grants = [("GRANT ALL PRIVILEGES ON *.* TO `u`@`%`",)]
    ok, why, _ = db.probe_account(
        as_real(FakeEngine(FakeConn(MY, [("CURRENT_USER", grants), ("applicable_roles", []), ("information_schema.routines", [])])))
    )
    assert ok is True and "grants include" in why
    script = [
        ("CURRENT_USER", [("GRANT USAGE ON *.* TO `u`@`%`",)]),
        ("applicable_roles", [("writer",)]),
        ("FOR writer", [("GRANT INSERT ON `d`.* TO `writer`",)]),
        ("information_schema.routines", [("shop", "fn", "DEFINER")]),
    ]
    ok, why, risks = db.probe_account(as_real(FakeEngine(FakeConn(MY, script))))
    assert ok is True and "role writer grants" in why and risks[0].names == ("fn",)
    ro = [("CURRENT_USER", [("GRANT SELECT ON `d`.* TO `u`@`%`",)]), ("applicable_roles", []), ("information_schema.routines", [])]
    assert db.probe_account(as_real(FakeEngine(FakeConn(MY, ro))))[0] is False


TIDY_ATTR = {
    "pymysql": ("_result", types.SimpleNamespace(_result=object())),
    "mysqldb": ("connection", types.SimpleNamespace(connection=object())),
    "mysqlconnector": ("_connection", types.SimpleNamespace(_connection=object())),
}


@pytest.mark.parametrize("driver", DRIVERS)
def test_offline_mysql_drivers_early_stop_no_drain_connection_dropped(driver: str, caplog: pytest.LogCaptureFixture) -> None:
    attr, cursor = TIDY_ATTR[driver]
    c = my_conn(driver, cursor, raw=Raw() if driver == "mysqlconnector" else object())
    reports: list[Any] = []
    with caplog.at_level("WARNING", logger="db_lens"):
        _, rows, trunc = db.fetch_limited(c, "q", 5, on_discard=reports.append)
    assert trunc and len(rows) == 5 and c.invalidated and c.events == ["invalidate"] and noop_closed(c)
    assert c.res.seen_on_close[attr] is None and reports[0].method == "driver" and caplog.records == []
    fresh = my_conn(driver, types.SimpleNamespace(), raw=object())  # a new connection after the abort is unaffected
    assert db.fetch_limited(fresh, "q", 50)[2] is False and not fresh.invalidated


@pytest.mark.parametrize("driver", DRIVERS)
def test_offline_mysql_driver_internals_gone_still_correct(driver: str, monkeypatch: pytest.MonkeyPatch) -> None:
    c = my_conn(driver, NoInternals(), raw=object())
    _, rows, trunc = db.fetch_limited(c, "q", 5)
    assert trunc and len(rows) == 5 and c.invalidated
    monkeypatch.setattr(db, "TIDY", {})
    c2 = my_conn(driver, types.SimpleNamespace(), raw=object())
    reports: list[Any] = []
    db.fetch_limited(c2, "q", 5, on_discard=reports.append)
    assert reports[0].method == "generic" and c2.invalidated


def test_offline_mysql_cli_limit_streams_and_drops_connection(runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    c = FakeConn(MY, [("@@session.transaction_read_only", [(1,)])])
    big = FakeResult([(i,) for i in range(1000)], ["n"])

    def execute(q: Any, params: Any = None, **kw: Any) -> Any:
        c.exec_kw.append(kw)
        c.log.append((str(q), params))
        return big if "big" in str(q) else FakeResult([(1,)])

    c.execute = execute  # type: ignore[method-assign]

    @contextmanager
    def session(profile: Any) -> Iterator[Any]:
        yield as_real(FakeEngine(c)), {}

    monkeypatch.setattr(cli, "session", session)
    out = runner("query", "SELECT n FROM big", "--limit", "5", "-o", "json")
    assert len(json.loads(out)) == 5 and c.invalidated and big.fetched == 6
    assert {"execution_options": {"stream_results": True}} in c.exec_kw


@pytest.mark.parametrize("driver", DRIVERS)
def test_offline_mysql_streaming_unavailable_still_correct(driver: str) -> None:
    d = types.SimpleNamespace(name="mysql", driver=driver, supports_server_side_cursors=True)
    c = my_conn(driver, types.SimpleNamespace(), raw=object())
    c.dialect = d
    inner = c.execute

    def execute(stmt: Any, execution_options: Any = None, **k: Any) -> Any:
        if execution_options:
            raise StatementError("cannot stream", "q", None, NotImplementedError())
        return inner(stmt)

    c.execute = execute
    _, rows, trunc = db.fetch_limited(c, "q", 5, stream=True)
    assert trunc and len(rows) == 5 and d.supports_server_side_cursors is False and c.invalidated


@pytest.mark.parametrize(
    ("driver", "module", "steps"),
    [("mysqldb", "MySQLdb.connections", ()), ("pymysql", "pymysql.connections", ()), ("mysqlconnector", "mysql.connector.connection", ("shutdown",))],
)
def test_offline_teardown_steps_per_driver_and_no_server_control_call(driver: str, module: str, steps: tuple[str, ...]) -> None:
    called: list[Any] = []
    raw = lookalike(module, called)
    c = my_conn(driver, types.SimpleNamespace(), raw=raw)
    report = db.hard_close(c, c.res)
    assert c.invalidated and report.steps[0] == "neuter-close" and tuple(s for s in report.steps if s in ("shutdown", "cext-close")) == steps
    assert bool(called) == bool(steps) and (driver != "mysqldb" or called == [])  # mysqlclient's shutdown() would stop the SERVER


def test_offline_mysql_connector_streams_on_this_sqlalchemy() -> None:
    d = mysqlconnector.dialect()
    db.enable_connector_streaming(d)
    assert d.supports_server_side_cursors is True and d.execution_ctx_cls.__name__ == "StreamingContext"


def test_offline_mysql_error_codes_normalised_per_driver() -> None:
    def err(module: str, **attrs: Any) -> BaseException:
        e: BaseException = type("OperationalError", (Exception,), {"__module__": module})(*attrs.pop("args", ()))
        for k, v in attrs.items():
            setattr(e, k, v)
        return e

    assert db.error_code(err("pymysql.err", args=(1045, "denied"))) == "MY-1045"
    assert db.error_code(err("MySQLdb", args=(1045, "denied"))) == "MY-1045"
    assert db.error_code(err("mysql.connector.errors", errno=1045)) == "MY-1045"


@pytest.mark.parametrize("scheme", ["mysql+pymysql", "mariadb+pymysql"])
def test_offline_mariadb_statement_timeout_behind_both_url_schemes(scheme: str) -> None:
    d: Any = make_url(f"{scheme}://u@h/d").get_dialect()()
    d.is_mariadb = True  # what SQLAlchemy learns from the server banner
    c = FakeConn(d, [("tx_read_only", [(1,)])])
    with db.connect_ro(as_real(FakeEngine(c)), timeout=2) as cc:
        assert "SET SESSION max_statement_time = 2.0" in fc(cc).sql


# ---------------------------------------------------------------------------------------------------------- TLS
@pytest.mark.parametrize("mode", dbconn.SSL_MODES)
def test_offline_pg_tls_modes_requested_and_verified(mode: str) -> None:
    assert dbconn.ssl_query("postgresql+psycopg2", mode) == {"sslmode": mode}
    ssl = pg_engine([("pg_stat_ssl", [(True,)])])
    if mode == "disable":
        return
    db.assert_tls(as_real(ssl), mode)
    with pytest.raises(db.TlsError, match="NOT encrypted"):
        db.assert_tls(as_real(pg_engine([("pg_stat_ssl", [(False,)])])), mode)


def patched_pg(monkeypatch: pytest.MonkeyPatch, encrypted: bool) -> None:
    patch_engine(monkeypatch, pg_engine([("pg_stat_ssl", [(encrypted,)]), ("SHOW transaction_read_only", [("on",)])]))
    monkeypatch.setattr(db, "probe_account", lambda e: (False, "", []))


def test_offline_pg_cli_connect_with_ssl_stores_and_enforces(home: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    args = ("connect", "-D", "postgresql", "--host", "127.0.0.1", "-d", "x", "-u", "bob", "--ssl-mode", "require", "-n", "p")
    patched_pg(monkeypatch, True)
    runner(*args, has="TLS: require (verified)")
    assert store.load()["profiles"]["p"]["tls"] == "require"
    patched_pg(monkeypatch, False)  # the server stops encrypting: every later command refuses
    runner("query", "SELECT 1", code=1, has="TlsError")


@pytest.mark.parametrize("driver", DRIVERS)
@pytest.mark.parametrize("mode", ["require", "disable"])
def test_offline_mysql_tls_require_and_disable_per_driver(driver: str, mode: str) -> None:
    q = dbconn.ssl_query(f"mysql+{driver}", mode)
    assert (q.get("ssl_disabled") == "true") == (mode == "disable" and driver != "mysqldb")
    if driver == "mysqldb":
        assert q == {"ssl_mode": dbconn._MYSQLDB_MODE[mode]}


@pytest.mark.parametrize("driver", DRIVERS)
@pytest.mark.parametrize("mode", ["verify-ca", "verify-full"])
def test_offline_mysql_tls_verify_modes_need_the_ca_and_reject_a_wrong_one(
    driver: str, mode: str, home: Path, runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    q = dbconn.ssl_query(f"mysql+{driver}", mode, ca="ca.pem")
    assert q["ssl_ca"] == "ca.pem"
    if driver != "mysqldb":
        with pytest.raises(ValueError, match="without --ssl-ca"):
            dbconn.ssl_query(f"mysql+{driver}", mode)

    class WrongCA(FakeEngine):
        def connect(self) -> Any:
            raise OperationalError("connect", {}, Exception("certificate verify failed"))

    patch_engine(monkeypatch, WrongCA(FakeConn(MY)))
    runner(
        "connect",
        "-D",
        "mysql",
        "--driver",
        driver,
        "--host",
        "127.0.0.1",
        "-d",
        "x",
        "-u",
        "u",
        "--ssl-mode",
        mode,
        "--ssl-ca",
        "wrong.pem",
        "-n",
        "m",
        code=1,
        has="certificate verify failed",
    )
    assert store.load()["profiles"] == {}


@pytest.mark.parametrize("cipher", ["", None])
def test_offline_driver_that_does_not_encrypt_is_caught_after_connect(cipher: str | None) -> None:
    rows = [("Ssl_cipher", cipher)] if cipher is not None else []
    eng = FakeEngine(FakeConn(MY, [("Ssl_cipher", rows)]))
    with pytest.raises(db.TlsError, match="NOT encrypted"):
        db.assert_tls(as_real(eng), "require")
    db.assert_tls(as_real(FakeEngine(FakeConn(MY, [("Ssl_cipher", [("Ssl_cipher", "TLS_AES_256_GCM_SHA384")])]))), "require")


# ---------------------------------------------------------------------------------------------------------- SSH tunnel
@pytest.mark.parametrize("which", ["pg", "my"])
def test_offline_ssh_tunnel_reaches_the_database_and_cleans_up(which: str, fake_ssh: Any) -> None:  # noqa: F811
    url = make_url("postgresql://u:pw@db.internal:5432/x" if which == "pg" else "mysql+pymysql://u:pw@db.internal:3306/x")
    with tunnel.forward(url, tunnel.SshSpec("bastion")) as routed:
        assert routed.port is not None and routed.port != url.port
        assert (routed.query["hostaddr"], routed.host) == ("127.0.0.1", "db.internal") if which == "pg" else routed.host == "127.0.0.1"
        assert len(fake_ssh.started) == 1 and not fake_ssh.started[0].stopped
    assert fake_ssh.started[0].stopped and fake_ssh.started[0].sock is None


def test_offline_ssh_cli_end_to_end_with_profile_commands(home: Path, runner: Any, fake_ssh: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    patch_engine(monkeypatch, pg_engine([("SHOW transaction_read_only", [("on",)])]))
    monkeypatch.setattr(db, "probe_account", lambda e: (False, "", []))
    runner("connect", "postgresql://bob@db.internal:5432/x", "--ssh", "me@bastion", "-n", "p", has="Connected p")
    assert json.loads(runner("profiles", "list", "-o", "json"))[0]["ssh"] == "me@bastion"
    assert json.loads(runner("query", "SELECT 1", "-o", "json")) == [{"c": 0}]
    assert len(fake_ssh.started) == 2 and all(p.stopped for p in fake_ssh.started)  # one tunnel per command, none left running


@pytest.mark.parametrize(
    ("stderr", "hint"), [("Permission denied (publickey).", "rejected the key"), ("Host key verification failed.", "--ssh-accept-new")]
)
def test_offline_ssh_failures_are_clean_panels(stderr: str, hint: str, home: Path, runner: Any, fake_ssh: Any) -> None:  # noqa: F811
    fake_ssh.failure = stderr
    runner("connect", "postgresql://bob@db.internal:5432/x", "--ssh", "bastion", code=1, has=hint)
    assert store.load()["profiles"] == {}


# ---------------------------------------------------------------------------------------------------------- completeness
EQUIVALENTS = {
    "test_pg_transaction_poisoning_is_real_and_catalog_recovers": "test_offline_pg_poisoning_and_catalog_recovery",
    "test_pg_readonly_session_is_effective_and_verified": "test_offline_pg_readonly_session_verified_and_writes_refused",
    "test_pg_server_side_cursor_leaves_no_orphan_backend": "test_offline_pg_server_side_cursor_is_closed_and_never_drained",
    "test_pg_write_capability_and_strict_mode": "test_offline_pg_write_capability_and_strict_mode",
    "test_mysql_readonly_session_is_effective_and_verified": "test_offline_mysql_readonly_session_verified",
    "test_mysql_write_capability": "test_offline_mysql_write_capability_and_roles_and_routines",
    "test_mysql_early_stop_is_fast_and_kills_the_server_query": "test_offline_mysql_drivers_early_stop_no_drain_connection_dropped",
    "test_mysql_cli_limit_on_huge_table_is_fast_and_connection_not_reused": "test_offline_mysql_cli_limit_streams_and_drops_connection",
    "test_mysql_drivers_early_stop_is_fast_streams_and_kills_the_query": "test_offline_mysql_drivers_early_stop_no_drain_connection_dropped",
    "test_mysql_drivers_log_nothing_and_fresh_connections_still_work_after_abort": "test_offline_mysql_drivers_early_stop_no_drain_connection_dropped",
    "test_mysql_driver_internals_gone_still_fast_and_exception_free": "test_offline_mysql_driver_internals_gone_still_correct",
    "test_pg_probe_sees_noinherit_role_reachable_by_set_role_and_routines": "test_offline_pg_probe_noinherit_roles_and_routines",
    "test_mysql_probe_resolves_non_default_roles_and_reports_visible_routines": "test_offline_mysql_write_capability_and_roles_and_routines",
    "test_pg_extension_functions_are_informational_custom_ones_are_named": "test_offline_pg_extension_functions_informational_custom_named",
    "test_pg_legacy_routine_query_is_valid_on_this_server": "test_offline_pg_legacy_routine_query_fallback",
    "test_pg_error_codes_are_normalised_from_psycopg2": "test_offline_pg_error_codes_normalised",
    "test_mysql_error_codes_are_normalised_per_driver": "test_offline_mysql_error_codes_normalised_per_driver",
    "test_mysql_still_correct_when_streaming_is_unavailable": "test_offline_mysql_streaming_unavailable_still_correct",
    "test_teardown_steps_per_driver_and_no_server_control_call": "test_offline_teardown_steps_per_driver_and_no_server_control_call",
    "test_mysql_connector_and_all_drivers_stream_on_every_sqlalchemy": "test_offline_mysql_connector_streams_on_this_sqlalchemy",
    "test_pg_statement_timeout_really_aborts_the_query": "test_offline_pg_statement_timeout_set_and_reported",
    "test_mariadb_statement_timeout_works_behind_mysql_and_mariadb_urls": "test_offline_mariadb_statement_timeout_behind_both_url_schemes",
    "test_pg_tls_modes_are_requested_and_verified": "test_offline_pg_tls_modes_requested_and_verified",
    "test_pg_cli_connect_with_ssl_stores_and_enforces_the_demand": "test_offline_pg_cli_connect_with_ssl_stores_and_enforces",
    "test_mysql_tls_require_and_disable_per_driver": "test_offline_mysql_tls_require_and_disable_per_driver",
    "test_mysql_tls_verify_modes_accept_the_right_ca_and_reject_the_wrong_one": "test_offline_mysql_tls_verify_modes_need_the_ca_and_reject_a_wrong_one",
    "test_a_driver_that_does_not_encrypt_by_default_is_caught_by_the_post_connect_check": "test_offline_driver_that_does_not_encrypt_is_caught_after_connect",
    "test_ssh_tunnel_reaches_the_database_and_cleans_up": "test_offline_ssh_tunnel_reaches_the_database_and_cleans_up",
    "test_ssh_cli_end_to_end_with_profile_commands": "test_offline_ssh_cli_end_to_end_with_profile_commands",
    "test_ssh_failures_are_clean_panels": "test_offline_ssh_failures_are_clean_panels",
}


def test_every_live_test_has_an_offline_equivalent() -> None:
    src = (Path(__file__).parent / "test_integration.py").read_text(encoding="utf-8")
    live = {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    assert live == set(EQUIVALENTS), live ^ set(EQUIVALENTS)
    assert all(callable(globals().get(v)) for v in EQUIVALENTS.values())
