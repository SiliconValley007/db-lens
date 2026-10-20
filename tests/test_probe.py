"""Account probing for PostgreSQL and MySQL/MariaDB (mock-driven) and UDF visibility in the SQL guard."""

from pathlib import Path
from typing import Any, cast

import pytest
from rich.text import Text
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.exc import SQLAlchemyError

from db_lens import cli, db, sqlguard

from .fakes import FakeConn, FakeEngine, as_real, new_dialect

MY, PG = new_dialect(mysql), new_dialect(postgresql)
MY.name = "mysql"


def my(script: Any) -> Any:
    return FakeEngine(FakeConn(MY, script))


def test_mysql_roles_are_resolved_and_a_writing_role_is_reported() -> None:
    eng = my(
        [
            ("SHOW GRANTS FOR CURRENT_USER()", [("GRANT USAGE ON *.* TO `u`@`%`",), ("GRANT `writer` TO `u`@`%`",)]),
            ("applicable_roles", [("writer",), ("reader",)]),
            ("SHOW GRANTS FOR writer", [("GRANT INSERT ON `d`.* TO `writer`",)]),
            ("SHOW GRANTS FOR reader", [("GRANT SELECT ON `d`.* TO `reader`",)]),
            ("information_schema.routines", []),
        ]
    )
    writable, why, risks = db.probe_account(eng)
    assert writable is True and "role writer grants INSERT" in why and risks == []


def test_mysql_unresolvable_role_is_a_risk_not_silence() -> None:
    eng = my(
        [
            ("SHOW GRANTS FOR CURRENT_USER()", [("GRANT SELECT ON `d`.* TO `u`@`%`",)]),
            ("applicable_roles", [("ghost",)]),
            ("SHOW GRANTS FOR ghost", SQLAlchemyError("1141: no such grant")),
            ("information_schema.routines", []),
        ]
    )
    writable, _, risks = db.probe_account(eng)
    assert writable is False and risks[0].kind == "unresolved-role" and "ghost" in risks[0].message and risks[0].blocking
    assert eng.conn.rollbacks == 1


def test_mysql_without_roles_support_still_works() -> None:
    eng = my(
        [
            ("SHOW GRANTS FOR CURRENT_USER()", [("GRANT SELECT ON `d`.* TO `u`@`%`",)]),
            ("applicable_roles", SQLAlchemyError("no such table (MySQL 5.7)")),
            ("information_schema.routines", []),
        ]
    )
    assert db.probe_account(eng) == (False, "", [])


def test_mysql_executable_routines_are_reported_with_definer_count() -> None:
    eng = my(
        [
            ("SHOW GRANTS FOR CURRENT_USER()", [("GRANT SELECT, EXECUTE ON `d`.* TO `u`@`%`",)]),
            ("applicable_roles", []),
            (
                "information_schema.routines",
                [("d", "a", "DEFINER"), ("d", "b", "DEFINER"), ("d", "c", "DEFINER"), ("d", "e", "INVOKER"), ("d", "f", "INVOKER")],
            ),
        ]
    )
    writable, _, risks = db.probe_account(eng)
    assert writable is False and risks[0].kind == "custom-routine" and risks[0].blocking
    assert "5 user-created routine(s)" in risks[0].message and "3 with DEFINER" in risks[0].message and risks[0].names == ("a", "b", "c", "e", "f")


def pg(*script: Any) -> Any:
    return FakeEngine(FakeConn(PG, list(script)))


# (ext, security_definer, count, sample, names) as returned by PG_ROUTINES
CUSTOM = (False, True, 3, ["app.sync", "app.purge"], ["sync", "purge", "audit"])
CUSTOM_INV = (False, False, 2, ["app.report"], ["report", "fmt"])
EXT = (True, False, 40, ["public.similarity"], ["similarity"])


def test_postgres_separates_user_created_routines_from_extension_functions() -> None:
    writable, why, risks = db.probe_account(pg(("pg_has_role", [(True,)]), ("pg_proc", [CUSTOM, CUSTOM_INV, EXT])))
    assert writable is True and "SET ROLE" in why
    custom = [r for r in risks if r.kind == "custom-routine"]
    ext = [r for r in risks if r.kind == "extension-routine"]
    assert len(custom) == 1 and custom[0].blocking and "5 user-created routine(s)" in custom[0].message
    assert "3 with DEFINER/SECURITY DEFINER" in custom[0].message and "app.sync" in custom[0].message
    assert set(custom[0].names) == {"sync", "purge", "audit", "report", "fmt"}
    assert len(ext) == 1 and not ext[0].blocking and "40 extension-provided" in ext[0].message and "informational" in ext[0].message


def test_postgres_extension_functions_alone_do_not_make_the_account_risky() -> None:
    writable, _, risks = db.probe_account(pg(("pg_has_role", [(False,)]), ("pg_proc", [EXT])))
    assert writable is False and [r.blocking for r in risks] == [False]
    assert cli.risk_panel(writable, "", risks) is None  # nothing to warn about: no noise


def test_postgres_nothing_executable_is_clean() -> None:
    assert db.probe_account(pg(("pg_has_role", [(False,)]), ("pg_proc", []))) == (False, "", [])


def test_postgres_pre11_fallback_uses_proisagg_when_prokind_is_missing() -> None:
    eng = pg(("pg_has_role", [(False,)]), ("prokind", SQLAlchemyError("column p.prokind does not exist")), ("pg_proc", [CUSTOM]))
    writable, _, risks = db.probe_account(eng)
    assert writable is False and risks[0].kind == "custom-routine" and eng.conn.rollbacks == 1
    assert "proisagg" in eng.conn.sql[-1] and "prokind" not in eng.conn.sql[-1]


def test_postgres_routines_unreadable_means_unknown_not_safe() -> None:
    writable, why, _ = db.probe_account(pg(("pg_has_role", [(False,)]), ("pg_proc", SQLAlchemyError("denied"))))
    assert writable is None and "executable routines" in why


def test_postgres_routine_sql_excludes_system_namespaces_triggers_and_extensions() -> None:
    q = db.PG_ROUTINES
    for needle in (
        "NOT IN ('pg_catalog', 'information_schema')",
        "pg\\_toast%",
        "pg\\_temp%",
        "'event_trigger'::regtype",
        "d.deptype = 'e'",
        "has_function_privilege(p.oid, 'EXECUTE')",
        "p.prokind IN ('f', 'p')",
    ):
        assert needle in q, needle


def test_risk_panel_groups_custom_roles_and_standard_separately() -> None:
    risks = [
        db.Risk("extension-routine", "40 extension-provided function(s)", False),
        db.Risk("custom-routine", "5 user-created routine(s)"),
        db.Risk("unresolved-role", "role(s) ghost could not be resolved"),
    ]
    panel = cli.risk_panel(False, "", risks)
    assert panel is not None
    text_ = cast(Text, panel.renderable).plain
    assert text_.index("Custom routines (user-created") < text_.index("Roles that could not") < text_.index("Standard / extension functions")
    assert "informational, not counted against --strict" in text_ and "SQL checks are best-effort" in text_
    write_panel = cli.risk_panel(True, "grants ALL", [])
    assert write_panel is not None and write_panel.title == "Write-capable account"
    assert cli.risk_panel(False, "", risks[:1]) is None


def test_stored_risks_legacy_strings_and_dicts_normalise() -> None:
    stored = [
        "old v0.6 text",
        {"kind": "extension-routine", "message": "x", "blocking": False, "names": []},
        {"kind": "custom-routine", "message": "y", "blocking": True, "names": ["f"]},
    ]
    assert [r["message"] for r in cli.blocking(stored)] == ["old v0.6 text", "y"]


def test_postgres_probe_sql_walks_the_role_hierarchy_by_oid() -> None:
    assert "pg_has_role(current_user, r.oid, 'MEMBER')" in db.PG_WRITE  # inherited AND SET ROLE-reachable roles
    assert "has_table_privilege(r.oid, c.oid, 'INSERT')" in db.PG_WRITE  # privileges of each reachable role
    assert "pg_write_all_data" in db.PG_WRITE and "deptype = 'e'" in db.PG_ROUTINES  # predefined roles; extension code skipped


def test_probe_sql_parses_for_each_dialect() -> None:
    import sqlglot

    sqlglot.parse_one(db.PG_WRITE, read="postgres")
    sqlglot.parse_one(db.PG_ROUTINES, read="postgres")
    sqlglot.parse_one(db.MY_ROUTINES, read="mysql")
    sqlglot.parse_one(db.MY_ROLES, read="mysql")


def test_unsupported_dialect_is_unknown() -> None:
    d = new_dialect(postgresql)
    d.name = "mssql"
    assert db.probe_account(as_real(FakeEngine(FakeConn(d))))[0] is None


# ---- guard: visibility into UDF calls ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql,expected",
    [
        ("select run_mutation_job()", ["run_mutation_job"]),
        ("select my_schema.do_work(1), upper(a), count(*) from t", ["do_work"]),
        ("select pg_size_pretty(pg_total_relation_size('t')), now(), coalesce(a, 1) from t", []),
        ("with x as (select hidden_udf(a) from t) select * from x", ["hidden_udf"]),
        ("select a, a, b_fn(1), b_fn(2) from t", ["b_fn"]),
        ("select * from table(dbms_xplan.display())", []),
    ],
)
def test_unknown_calls(sql: Any, expected: Any) -> None:
    assert sqlguard.classify_calls(sqlguard.validate(sql, "postgresql" if "xplan" not in sql else "oracle"))[1] == expected


def test_cli_warns_about_udf_calls_and_strict_refuses_them(prof: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    runner("query", "SELECT my_udf(1)", code=1, has="calls my_udf")  # warned before it even runs
    monkeypatch.setattr(db, "probe_account", lambda e: (False, "", []))
    runner("query", "SELECT my_udf(1)", "--strict", code=1, has="statement calls my_udf")
    runner("query", "SELECT upper('a') AS u", "--strict", has="A")


def test_classify_calls_separates_known_custom_routines_from_unknown() -> None:
    root = sqlguard.validate("select app.sync(1), mystery(2), upper(a), count(*) from t", "postgresql")
    assert sqlguard.classify_calls(root, {"SYNC"}) == (["sync"], ["mystery"])  # case-insensitive match on bare name
    assert sorted(sqlguard.classify_calls(root)[1]) == ["mystery", "sync"] and sqlguard.classify_calls(root)[0] == []


def test_cli_distinguishes_custom_routine_calls_from_unknown_ones(prof: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from db_lens import store

    cfg = store.load()
    cfg["profiles"]["shop"]["risks"] = [{"kind": "custom-routine", "message": "1 routine", "blocking": True, "names": ["sync_orders"]}]
    store.save(cfg)
    runner("query", "SELECT sync_orders(1)", code=1, has="user-created routine(s) sync_orders")
    assert "calls sync_orders: if user-defined" not in runner.last.output  # classified as certain, not merely "unknown"
    runner("query", "SELECT other_fn(1)", code=1, has="calls other_fn: if user-defined")
    monkeypatch.setattr(db, "probe_account", lambda e: (False, "", []))
    runner("query", "SELECT sync_orders(1)", "--strict", code=1, has="statement calls sync_orders")


def test_mysql_without_roles_view_reports_roles_found_in_show_grants() -> None:
    """Vanilla MySQL may lack information_schema.applicable_roles: roles must be reported as unresolved, never assumed safe."""
    eng = my(
        [
            (
                "SHOW GRANTS FOR CURRENT_USER()",
                [("GRANT USAGE ON *.* TO `u`@`%`",), ("GRANT SELECT ON `d`.* TO `u`@`%`",), ("GRANT `writer`@`%`, `auditor`@`%` TO `u`@`%`",)],
            ),
            ("applicable_roles", SQLAlchemyError("Table 'information_schema.applicable_roles' doesn't exist")),
            ("information_schema.routines", []),
        ]
    )
    writable, _, risks = db.probe_account(eng)
    assert writable is False and len(risks) == 1 and risks[0].kind == "unresolved-role" and risks[0].blocking
    assert "writer, auditor" in risks[0].message and "not analysed" in risks[0].message


def test_mysql_without_roles_view_and_without_roles_is_clean() -> None:
    eng = my(
        [
            ("SHOW GRANTS FOR CURRENT_USER()", [("GRANT SELECT ON `d`.* TO `u`@`%`",)]),
            ("applicable_roles", SQLAlchemyError("no such view")),
            ("information_schema.routines", []),
        ]
    )
    assert db.probe_account(eng) == (False, "", [])


@pytest.mark.parametrize(
    "grants,names",
    [
        (["GRANT `a`@`%` TO `u`@`%`"], ["a"]),
        (["GRANT `a`, `b` TO `u`@`%`"], ["a", "b"]),
        (["GRANT SELECT ON `d`.* TO `u`@`%`", "GRANT USAGE ON *.* TO `u`@`%`"], []),
        (["GRANT `a`@`%` TO `u`@`%`", "GRANT `a`@`%` TO `u`@`%`"], ["a"]),
    ],
)
def test_mysql_role_grant_parser(grants: Any, names: Any) -> None:
    assert db.mysql_role_grants(grants) == names
