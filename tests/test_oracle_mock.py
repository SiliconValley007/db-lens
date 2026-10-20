"""Oracle has no test server here, so its session/catalog/privilege logic is verified against a scripted fake that records the
exact statements, bind parameters and rollbacks, and replays Oracle-shaped errors (ORA-xxxxx)."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import oracle

from db_lens import db, sqlguard

from .fakes import FakeConn, FakeEngine, as_real, fc, new_dialect, ora

D = new_dialect(oracle)


def engine(script: Any = (), **kw: Any) -> Any:
    return FakeEngine(FakeConn(D, script, **kw))


# ---- session: ALTER SESSION + READ ONLY ---------------------------------------------------------------------------
def test_session_statements_order_and_formatting() -> None:
    eng = engine()
    with db.connect_ro(eng, schema="scott", timeout=2.5) as c:
        assert fc(c).sql == ["ALTER SESSION SET CURRENT_SCHEMA = scott", "SET TRANSACTION READ ONLY"]  # schema first, RO first-in-txn
        assert fc(c).connection.driver_connection.call_timeout == 2500  # ms, applied on the driver
    assert not any("SELECT" in q for q in eng.conn.sql)  # Oracle has no unprivileged verification query


@pytest.mark.parametrize(
    "schema,rendered", [("HR", '"HR"'), ("scott", "scott"), ("MixedCase", '"MixedCase"'), ('x"; DROP USER y; --', '"x""; DROP USER y; --"')]
)
def test_schema_identifier_quoting(schema: Any, rendered: Any) -> None:
    eng = engine()
    with db.connect_ro(eng, schema=schema) as c:
        assert fc(c).sql[0] == f"ALTER SESSION SET CURRENT_SCHEMA = {rendered}"
    assert eng.conn.sql[1:] == ["SET TRANSACTION READ ONLY"]  # nothing smuggled in after the identifier


def test_read_only_failure_is_fatal_and_names_the_ora_error() -> None:
    eng = engine([("READ ONLY", ora(1453, "SET TRANSACTION must be first statement of transaction"))])
    with pytest.raises(db.SessionError, match=r"ORA-01453.*Refusing"), db.connect_ro(eng):
        pytest.fail("user SQL must not run")


def test_schema_failure_stops_before_read_only_is_attempted() -> None:
    eng = engine([("CURRENT_SCHEMA", ora(1435, "user does not exist"))])
    with pytest.raises(db.SessionError, match="switch to schema 'nobody'"), db.connect_ro(eng, schema="nobody"):
        pytest.fail("unreachable")
    assert eng.conn.sql == ["ALTER SESSION SET CURRENT_SCHEMA = nobody"]


def test_timeout_driver_without_call_timeout_degrades_to_a_notice() -> None:
    class Frozen:
        __slots__ = ()  # assigning call_timeout raises AttributeError

    eng = engine(driver_connection=Frozen())
    with db.connect_ro(eng, timeout=1) as c:
        assert [n.code for n in c.info["warnings"]] == ["TIMEOUT-UNSUPPORTED"]
    assert eng.conn.sql == ["SET TRANSACTION READ ONLY"]  # read-only was still enabled first


def test_execution_never_needs_a_postgres_option_on_oracle() -> None:
    called: list[Any] = []
    eng = engine()

    def execution_options(**k: Any) -> Any:
        called.append(k)
        return eng.conn

    eng.conn.execution_options = execution_options
    with db.connect_ro(eng):
        pass
    assert called == []


# ---- catalog estimates: case folding, tiers, failure paths --------------------------------------------------------
ROWS = [("EMP", 14, 65536), ("MixedCase", None, None), ("DEPT", 4, None)]


def test_catalog_tier_one_privilege_error_falls_back_and_rolls_back() -> None:
    eng = engine([("all_segments", ora(942, "table or view does not exist")), ("FROM all_tables", ROWS)])
    est = db.estimates(eng, "scott", ["emp", "MixedCase", "dept"])
    assert est == {"emp": (14, 65536), "MixedCase": (None, None), "dept": (4, None)}  # EMP->emp; mixed case kept exact
    assert eng.conn.rollbacks == 1
    assert [p for _, p in eng.conn.log] == [{"s": "SCOTT"}, {"s": "SCOTT"}]  # scott -> SCOTT bound to :s
    assert all("NOT LIKE 'BIN$%'" in q for q in eng.conn.sql)  # recycle-bin tables excluded


def test_catalog_quoted_mixed_case_schema_is_not_uppercased() -> None:
    eng = engine([("FROM all_tables", [])])
    db.estimates(eng, "MixedSchema", [])
    assert eng.conn.log[0][1] == {"s": "MixedSchema"}


def test_catalog_all_tiers_failing_returns_empty_and_never_raises() -> None:
    eng = engine([("_tables", ora(942))])
    assert db.estimates(eng, "scott", ["emp"]) == {} and eng.conn.rollbacks == 3


def test_catalog_survives_connection_errors() -> None:
    class Down(FakeEngine):
        def connect(self) -> Any:
            raise ora(12541, "TNS:no listener")

    assert db.estimates(as_real(Down(FakeConn(D))), "scott", ["emp"]) == {}


# ---- account probe -----------------------------------------------------------------------------------------------
def scripted(**counts: Any) -> Any:
    """counts: write_sys / write_tab / owns / execute / roles -> value or Exception, keyed on unique substrings."""
    keys = {
        "write_sys": "FROM session_privs",
        "write_tab": "FROM all_tab_privs WHERE grantee",
        "owns": "FROM user_tables",
        "execute": "privilege = 'EXECUTE'",
        "roles": "FROM user_role_privs",
    }
    return [(keys[k], v if isinstance(v, Exception) else [(v,)]) for k, v in counts.items()]


def test_probe_clean_read_only_account() -> None:
    eng = engine(scripted(write_sys=0, write_tab=0, owns=0, execute=0, roles=0))
    assert db.probe_account(eng) == (False, "", [])


def test_create_session_alone_is_not_a_write_privilege() -> None:
    sys_query = next(q for q, kind, _ in db.ORA_PROBES if "session_privs" in q)
    assert "privilege <> 'CREATE SESSION'" in sys_query  # v0.5.0 flagged every Oracle login as write-capable


@pytest.mark.parametrize("key,why", [("write_sys", "system privileges"), ("write_tab", "INSERT/UPDATE/DELETE"), ("owns", "owns tables")])
def test_probe_detects_each_write_path(key: Any, why: Any) -> None:
    eng = engine(scripted(**{"write_sys": 0, "write_tab": 0, "owns": 0, key: 1}))
    writable, reason, _ = db.probe_account(eng)
    assert writable is True and why in reason


def test_probe_reports_executable_routines_and_unresolved_roles_as_risks() -> None:
    eng = engine(scripted(write_sys=0, write_tab=0, owns=0, execute=7, roles=2))
    writable, _, risks = db.probe_account(eng)
    assert writable is False
    assert any("7 user-defined routines" in r.message and "SQL guard cannot see inside" in r.message and r.blocking for r in risks)
    assert any("2 granted roles are not enabled by default" in r.message and "SET ROLE" in r.message for r in risks)


def test_probe_unreadable_views_mean_unknown_not_safe() -> None:
    eng = engine(scripted(write_sys=ora(942), write_tab=0, owns=0, execute=0, roles=0))
    writable, reason, _ = db.probe_account(eng)
    assert writable is None and "could not inspect" in reason and "system privileges" in reason
    assert eng.conn.rollbacks == 1


def test_probe_found_write_privilege_wins_over_unreadable_views() -> None:
    eng = engine(scripted(write_sys=ora(942), write_tab=1, owns=0, execute=0, roles=0))
    assert db.probe_account(eng)[0] is True


def test_probe_connection_failure_is_unknown() -> None:
    class Down(FakeEngine):
        def connect(self) -> Any:
            raise ora(1017, "invalid username/password")

    writable, reason, risks = db.probe_account(as_real(Down(FakeConn(D))))
    assert writable is None and "ORA-01017" in reason and risks == []


def test_every_oracle_probe_is_valid_oracle_sql() -> None:
    import sqlglot

    for q, _, _ in db.ORA_PROBES:
        sqlglot.parse_one(q, read="oracle")


# ---- guard: Oracle dialect parity ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "ALTER SESSION SET CURRENT_SCHEMA = hr",
        "SET TRANSACTION READ WRITE",
        "begin null; end;",
        "EXPLAIN PLAN FOR SELECT 1 FROM dual",
        "SELECT dbms_lock.sleep(1) FROM dual",
        "SELECT * FROM t FOR UPDATE",
        "MERGE INTO t USING s ON (1=1) WHEN MATCHED THEN UPDATE SET a=1",
    ],
)
def test_guard_rejects_oracle_write_and_session_statements(sql: Any) -> None:
    with pytest.raises(sqlguard.UnsafeSQL):
        sqlguard.validate(sql, "oracle")


def test_guard_allows_oracle_read_helpers() -> None:
    sqlguard.validate("SELECT * FROM table(dbms_xplan.display_cursor(NULL, NULL, 'TYPICAL'))", "oracle")
    sqlguard.validate("SELECT * FROM all_tables WHERE owner = 'HR' FETCH FIRST 10 ROWS ONLY", "oracle")


# ---- graceful degradation & error normalisation ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "text_,code",
    [
        ("ORA-01031: insufficient privileges", "ORA-01031"),
        ("DPY-4024: call timeout of 1000 ms exceeded", "DPY-4024"),
        ("DPI-1080: connection was closed by ORA-03113", "DPI-1080"),
        ("something else", None),
    ],
)
def test_error_code_normalises_oracle_driver_messages(text_: Any, code: Any) -> None:
    assert db.error_code(Exception(text_)) == code


def test_error_code_other_drivers() -> None:
    pg = types_ns(pgcode="42501")
    assert db.error_code(types_ns(orig=pg)) == "SQLSTATE-42501"

    class OperationalError(Exception):
        __module__ = "pymysql.err"

    assert db.error_code(OperationalError(1044, "denied")) == "MY-1044"
    e: Any = OperationalError("x")
    e.errno = 2013
    assert db.error_code(e) == "MY-2013"
    assert db.error_code(OSError(2, "nope")) is None  # errno-bearing exceptions from other libraries are not MySQL


def types_ns(**kw: Any) -> Any:
    return SimpleNamespace(**kw)


def test_alter_session_privilege_error_is_a_structured_notice_not_a_halt(caplog: pytest.LogCaptureFixture) -> None:
    eng = engine([("CURRENT_SCHEMA", ora(1031, "insufficient privileges"))])
    with caplog.at_level("WARNING", logger="db_lens"), db.connect_ro(eng, schema="hr", timeout=1) as c:
        (n,) = c.info["warnings"]
        assert (n.code, "hr" in n.message) == ("SCHEMA-NOT-SWITCHED", True) and str(n).endswith("[SCHEMA-NOT-SWITCHED]")
        assert eng.conn.sql[:2] == ["ALTER SESSION SET CURRENT_SCHEMA = hr", "SET TRANSACTION READ ONLY"]  # RO still enabled
    assert eng.conn.rollbacks == 1  # failed statement did not leave transaction state behind
    assert [cast(Any, r).code for r in caplog.records] == ["SCHEMA-NOT-SWITCHED"]


@pytest.mark.parametrize("err", [ora(1435, "user does not exist"), ora(942), ora(900, "invalid SQL statement")])
def test_other_schema_errors_remain_fatal(err: Any) -> None:
    eng = engine([("CURRENT_SCHEMA", err)])
    with pytest.raises(db.SessionError, match="switch to schema"), db.connect_ro(eng, schema="hr"):
        pytest.fail("unreachable")


@pytest.mark.parametrize("code", [1031, 1453, 1456])
def test_read_only_failure_is_fatal_even_for_privilege_errors(code: Any) -> None:
    eng = engine([("READ ONLY", ora(code))])
    with pytest.raises(db.SessionError, match=f"ORA-{code:05d}"), db.connect_ro(eng):
        pytest.fail("never degrade the read-only guarantee")


def test_catalog_third_tier_uses_user_tables_when_all_views_are_blocked() -> None:
    eng = engine([("all_tables", ora(942)), ("user_tables", [("EMP", 14, None)])])
    assert db.estimates(eng, "scott", ["emp"]) == {"emp": (14, None)} and eng.conn.rollbacks == 2
    assert "USER_TABLES".lower() in eng.conn.sql[-1].lower() and eng.conn.log[-1][1] == {"s": "SCOTT"}


def test_catalog_decimal_float_and_null_values_render_in_tables_listing() -> None:
    from decimal import Decimal

    eng = engine([("FROM all_tables", [("EMP", Decimal("1234"), Decimal("65536.0")), ("DEPT", 4.0, None), ("BONUS", None, None)])])
    est: Any = db.estimates(eng, "scott", ["emp", "dept", "bonus"])
    from db_lens import cli

    assert [int(est[n][0]) if est[n][0] is not None else None for n in ("emp", "dept", "bonus")] == [1234, 4, None]
    assert cli.human(int(est["emp"][1])) == "64 KB"


def test_every_oracle_catalog_tier_is_valid_oracle_sql() -> None:
    import sqlglot

    for q in db.EST["oracle"]:
        sqlglot.parse_one(q.replace(":s", "'X'"), read="oracle")


def test_catalog_unexpected_row_shape_is_treated_as_a_failed_tier() -> None:
    eng = engine([("all_segments", [("EMP",)]), ("FROM all_tables", [("EMP", 1, None)])])  # tier 1 returns a malformed row
    assert db.estimates(eng, "scott", ["emp"]) == {"emp": (1, None)} and eng.conn.rollbacks == 1
