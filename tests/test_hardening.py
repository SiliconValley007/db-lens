import csv
import io
import json
import logging
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from db_lens import cli, db, sqlguard, store

from .fakes import as_real, new_dialect


# ---- 5. CSV formula injection ------------------------------------------------------------------------------------
@pytest.mark.parametrize("v", ["=1+1", "+1", "-1+2", "@SUM(A1)", "\tcmd", "\rcmd", "=cmd|' /C calc'!A0"])
def test_csv_safe_prefixes_every_trigger(v: Any) -> None:
    assert cli.csv_safe(v) == "'" + v


@pytest.mark.parametrize("v", ["safe", "a=b", " =1", "", "x-", 5, -5, 1.5, None, b"=x"])
def test_csv_safe_leaves_everything_else(v: Any) -> None:
    assert cli.csv_safe(v) == v


def test_csv_export_sanitises_cells_and_headers_but_not_numbers(prof: Path, runner: Any) -> None:
    sql = "SELECT '=1+1' AS \"=HYPERLINK(1)\", '+3' AS b, '-4' AS c, '@x' AS d, char(9)||'t' AS e, char(13)||'r' AS f, -5 AS n, 'ok' AS g"
    rows = list(csv.reader(io.StringIO(runner("query", sql, "-o", "csv"))))
    assert rows[0][0] == "'=HYPERLINK(1)"  # header aliases are attacker-controlled too
    assert rows[1] == ["'=1+1", "'+3", "'-4", "'@x", "'\tt", "'\rr", "-5", "ok"]


def test_csv_raw_opt_out_and_json_untouched(prof: Path, runner: Any) -> None:
    assert runner("query", "SELECT '=1' AS a", "-o", "csv", "--csv-raw").splitlines()[1] == "=1"
    assert json.loads(runner("query", "SELECT '=1' AS a", "-o", "json")) == [{"a": "=1"}]


# ---- 1. transaction poisoning in catalog fallbacks ----------------------------------------------------------------
class PoisonConn:
    """Mimics PostgreSQL: after any error every statement fails until rollback()."""

    dialect = new_dialect(postgresql)

    def __init__(self) -> None:
        self.poisoned, self.rollbacks = False, 0

    def execute(self, q: Any, params: Any = None) -> Any:
        if self.poisoned:
            raise SQLAlchemyError("current transaction is aborted, commands ignored until end of transaction block")
        if "bad" in str(q):
            self.poisoned = True
            raise SQLAlchemyError("permission denied")
        return [("users", 5, 100)]

    def rollback(self) -> Any:
        self.poisoned, self.rollbacks = False, self.rollbacks + 1


def test_catalog_rolls_back_between_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(db.EST, "postgresql", ("SELECT bad FROM x WHERE :s", "SELECT good WHERE :s"))
    c = PoisonConn()
    assert db._catalog(as_real(c), "public") == {"users": (5, 100)} and c.rollbacks == 1


def test_catalog_all_tiers_failing_leaves_connection_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(db.EST, "postgresql", ("SELECT bad1 WHERE :s", "SELECT bad2 WHERE :s"))
    c = PoisonConn()
    assert db._catalog(as_real(c), "public") == {} and not c.poisoned and c.rollbacks == 2


def test_exact_counts_recover_after_a_poisoned_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    class C(PoisonConn):
        def execute(self, q: Any, params: Any = None) -> Any:
            if self.poisoned:
                raise SQLAlchemyError("aborted")
            if "UNION" in str(q.compile(dialect=self.dialect)).upper():
                self.poisoned = True
                raise SQLAlchemyError("boom")
            return types.SimpleNamespace(scalar=lambda: 3)

    c = C()
    assert db._counts(as_real(c), None, ["a", "b"]) == {"a": (3, None), "b": (3, None)} and c.rollbacks == 1


# ---- 2. session setup is explicit, verified, fail-closed ----------------------------------------------------------
@pytest.fixture
def eng(dbfile: Path) -> Iterator[Any]:
    with db.engine(make_url(f"sqlite:///{dbfile}")) as e:
        yield e


def test_unverifiable_readonly_flag_refuses_to_run(eng: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(db.RO, "sqlite", "SELECT 1")  # statement "works" but the flag is not on
    with pytest.raises(db.SessionError, match="not in effect"), db.connect_ro(eng):
        pytest.fail("user SQL must never run")


def test_failing_session_statement_is_a_clear_error(eng: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(db.RO, "sqlite", "SELEC nonsense")
    with pytest.raises(db.SessionError, match="enable the read-only session"), db.connect_ro(eng):
        pytest.fail("unreachable")


def test_verify_query_failing_everywhere_refuses(eng: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(db.VERIFY, "sqlite", ("SELEC 1",))
    with pytest.raises(db.SessionError, match="Could not confirm"), db.connect_ro(eng):
        pytest.fail("unreachable")


def test_timeout_failure_fatal_by_default_but_warning_on_mysql_like(eng: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(db.TMO, "sqlite", lambda ms: "SELEC bad")
    with pytest.raises(db.SessionError, match="timeout"), db.connect_ro(eng, timeout=1):
        pytest.fail("unreachable")
    monkeypatch.setattr(db, "TIMEOUT_NONFATAL", {"sqlite"})
    with db.connect_ro(eng, timeout=1) as c:
        assert "TIMEOUT-UNSUPPORTED" in str(c.info["warnings"][0])


def test_cli_reports_session_failure_as_panel(prof: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(db.RO, "sqlite", "SELECT 1")
    runner("query", "SELECT 1", code=1, has="SessionError")


# ---- 2/3. result cleanup and early-abort -------------------------------------------------------------------------
def test_fetch_limited_closes_result_and_truncates(eng: Any) -> None:
    with db.connect_ro(eng) as c:
        cols, rows, trunc = db.fetch_limited(c, text("SELECT id FROM users ORDER BY id"), 2)
        assert (cols, rows, trunc) == (["id"], [(1,), (2,)], True)
        _, rows, trunc = db.fetch_limited(c, text("SELECT id FROM users WHERE id = 1"), 5)
        assert rows == [(1,)] and not trunc


def test_fetch_limited_closes_result_even_when_fetch_fails(eng: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    closed = []
    with db.connect_ro(eng) as c:
        real = c.execute

        def spy(*a: Any, **k: Any) -> Any:
            r: Any = real(*a, **k)
            orig = r.close

            def close() -> None:
                closed.append(1)
                orig()

            def fetchmany(n: int | None = None) -> Any:
                raise RuntimeError("network")

            r.close, r.fetchmany = close, fetchmany
            return r

        monkeypatch.setattr(c, "execute", spy)
        with pytest.raises(RuntimeError):
            db.fetch_limited(c, text("SELECT 1"), 1)
    assert closed


def test_mysql_early_stop_skips_drain_and_drops_connection() -> None:
    drained = []
    cursor = types.SimpleNamespace(_result=object())
    res = types.SimpleNamespace(cursor=cursor, keys=lambda: ["a"], fetchmany=lambda n: [(i,) for i in range(n)], __enter__=None)

    class Res(types.SimpleNamespace):
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: Any) -> Any:
            drained.append(cursor._result)  # what the driver's close() would see

    r = Res(**res.__dict__)
    c = types.SimpleNamespace(
        execute=lambda s, **k: r, dialect=types.SimpleNamespace(name="mysql", driver="pymysql"), invalidate=lambda: drained.append("invalidated")
    )
    cols, rows, trunc = db.fetch_limited(as_real(c), "q", 3)
    assert trunc and len(rows) == 3 and drained == ["invalidated", None]  # connection dropped, nothing left to drain


def test_other_drivers_and_dialects_do_not_invalidate(eng: Any) -> None:
    with db.connect_ro(eng) as c:
        db.fetch_limited(c, text("SELECT id FROM users"), 1)
        assert not c.invalidated


# ---- 4. UDF boundary: privilege probe, warnings, --strict ---------------------------------------------------------
@pytest.mark.parametrize(
    "grants,hit",
    [
        (["GRANT SELECT ON `d`.* TO `u`@`%`"], None),
        (["GRANT USAGE ON *.* TO `u`@`%` IDENTIFIED BY PASSWORD 'x'", "GRANT SELECT, SHOW VIEW ON `d`.* TO `u`@`%`"], None),
        (["GRANT ALL PRIVILEGES ON *.* TO `u`@`%`"], "ALL PRIVILEGES"),
        (["GRANT SELECT, INSERT (a, b) ON `d`.`t` TO `u`@`%`"], "INSERT"),
        (["GRANT SELECT, CREATE ROUTINE ON `d`.* TO `u`@`%`"], "CREATE ROUTINE"),
    ],
)
def test_mysql_grant_parser(grants: Any, hit: Any) -> None:
    assert db.mysql_write_grants(grants) == hit


def test_sqlite_write_capability(dbfile: Path, tmp_path: Path) -> None:
    with db.engine(make_url(f"sqlite:///{dbfile}")) as e:
        assert db.probe_account(e)[0] is True  # a writable file is the residual risk
    with db.engine(make_url("sqlite://")) as e:
        assert db.probe_account(e)[0] is None


def test_notice_and_help_state_the_limitation(runner: Any) -> None:
    assert "read-only database role" in sqlguard.NOTICE and "UDF" in sqlguard.NOTICE
    out = runner("query", "--help", has=["read-only", "--strict"])
    assert "UDFs" in " ".join(out.split())


def test_connect_records_write_capability_and_profiles_shows_it(home: Path, dbfile: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "probe_account", lambda e: (True, "grants include ALL", []))
    runner("connect", f"sqlite:///{dbfile}", "-n", "w", hasnt="Write-capable account")  # SQLite: no account concept, no panel
    runner("profiles", has="WRITE-CAPABLE")


def test_query_warns_when_profile_is_write_capable_and_strict_refuses(prof: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from db_lens import store

    cfg = store.load()
    cfg["profiles"]["shop"]["writable"] = True
    store.save(cfg)

    def probe(r: Any) -> Any:
        def inner(e: Any) -> Any:
            return r

        return inner

    monkeypatch.setattr(db, "probe_account", probe((True, "grants include ALL", [])))
    runner("query", "SELECT 1 AS x", "--strict", code=1, has="not verifiably read-only")
    monkeypatch.setattr(db, "probe_account", probe((False, "", [])))
    runner("query", "SELECT 1 AS x", "--strict", has="x")
    monkeypatch.setattr(db, "probe_account", probe((None, "could not determine", [])))
    runner("query", "SELECT 1 AS x", "--strict", code=1, has="could not determine")
    monkeypatch.setattr(db, "probe_account", probe((False, "", [db.Risk("custom-routine", "2 user-created routine(s) executable")])))
    runner("query", "SELECT 1 AS x", "--strict", code=1, has="routine")
    monkeypatch.setattr(db, "probe_account", probe((False, "", [db.Risk("extension-routine", "40 extension functions", False)])))
    runner("query", "SELECT 1 AS x", "--strict", has="x")  # informational risks never block      # executable routines also fail --strict


# ---- zero credential leakage: stderr, stdout, logs, tracebacks ---------------------------------------------------------
SECRET = "Zx9!hunter2-Q"


def _no_leak(text: str) -> None:
    for form in (SECRET, "hunter2-Q"):
        assert form not in text, text


def test_connect_failure_never_prints_the_password_or_a_traceback(home: Path, runner: Any) -> None:
    runner("connect", f"postgresql+nodriver://bob:{SECRET}@127.0.0.1:1/x", code=1)
    _no_leak(runner.last.output)
    assert "Traceback" not in runner.last.output


def test_connect_with_unparsable_uri_redacts_the_echoed_url(home: Path, runner: Any) -> None:
    runner("connect", f"notascheme//bob:{SECRET}@host/db", "--password-stdin", code=1, inp="p\n")
    _no_leak(runner.last.output)
    runner("connect", f"postgresql://bob:{SECRET}@@@host:notaport/db", code=1)
    _no_leak(runner.last.output)


def test_password_from_stdin_is_registered_before_any_error_can_echo_it(home: Path, runner: Any) -> None:
    runner("connect", "-D", "postgresql", "--host", "127.0.0.1", "--port", "1", "-d", "x", "-u", "bob", "--password-stdin", code=1, inp=f"{SECRET}\n")
    _no_leak(runner.last.output)


def test_verbose_logs_are_redacted_and_carry_no_tracebacks(home: Path, dbfile: Path, runner: Any, caplog: pytest.LogCaptureFixture) -> None:
    store.remember(SECRET)
    logger = logging.getLogger("db_lens")
    cli.setup_logging(True)
    try:
        handler = next(h for h in logger.handlers if getattr(h, "_dbl_verbose", False))
        buf = io.StringIO()
        handler.setStream(buf)  # type: ignore[attr-defined]
        try:
            raise RuntimeError(f"driver said password={SECRET}")
        except RuntimeError:
            logger.debug("failure for %s", f"postgresql://u:{SECRET}@h/d", exc_info=True)
        logger.debug("note", extra={"code": "X"})  # library notices are dropped (the CLI prints them itself)
    finally:
        cli.setup_logging(False)
    out = buf.getvalue()
    _no_leak(out)
    assert "Traceback" not in out and "RuntimeError" not in out and "note" not in out and "***" in out


def test_profiles_listing_never_shows_a_stored_plaintext_password(home: Path, runner: Any) -> None:
    store.add("p", f"postgresql://bob:{SECRET}@h/d", plaintext=True)
    for fmt in ("table", "json", "csv"):
        runner("profiles", "list", "-o", fmt, hasnt=["hunter2"])


def test_run_entry_point_prints_a_redacted_panel_and_no_traceback(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    store.remember(SECRET)

    def boom(**_k: object) -> None:
        raise RuntimeError(f"cannot open postgresql://bob:{SECRET}@h/d")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exit_info:
        cli.run()
    seen = capsys.readouterr()
    assert exit_info.value.code == 1
    _no_leak(seen.out + seen.err)
    assert "Traceback" not in seen.out + seen.err and "RuntimeError" in seen.err + seen.out


def test_run_entry_point_passes_normal_exits_through(monkeypatch: pytest.MonkeyPatch) -> None:
    def fine(**_k: object) -> None:
        raise SystemExit(0)

    monkeypatch.setattr(cli, "app", fine)
    with pytest.raises(SystemExit) as exit_info:
        cli.run()
    assert exit_info.value.code == 0


def test_typer_never_shows_locals_in_tracebacks() -> None:
    assert cli.app.pretty_exceptions_show_locals is False
    assert cli.profiles_app.pretty_exceptions_show_locals is False
