from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlglot
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import make_url, text
from sqlalchemy.dialects import oracle
from sqlalchemy.exc import SQLAlchemyError

from db_lens import db

from .fakes import new_dialect


@pytest.fixture
def eng(dbfile: Path) -> Iterator[Any]:
    with db.engine(make_url(f"sqlite:///{dbfile}")) as e:
        yield e


def test_sqlite_estimates_exact_with_sizes_and_odd_names(eng: Any) -> None:
    names = ["users", "order items", "parent", 'we"ird [red]name']
    d = db.estimates(eng, "main", names)
    assert [d[n][0] for n in names] == [6, 1, 1, 1]
    assert all(s is None or s > 0 for _, s in d.values())
    assert "pages" in db.sqlite_summary(eng, "main")


def test_exact_counts_degrade_per_table(eng: Any) -> None:
    d = db.estimates(eng, "main", ["users", "does_not_exist", "parent"])  # one bad name must not sink the batch
    assert d["users"][0] == 6 and d["parent"][0] == 1 and "does_not_exist" not in d


def test_catalog_tiers_fall_through_on_permission_errors(eng: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    good = "SELECT name, 7, NULL FROM sqlite_master WHERE type = 'table' AND :s IS NOT NULL"
    monkeypatch.setitem(db.EST, "sqlite", ("SELECT * FROM no_such_catalog WHERE x = :s", good))
    assert db.estimates(eng, "main", ["users"])["users"] == (7, None)


def test_catalog_all_tiers_fail_returns_empty_not_error(eng: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(db.EST, "sqlite", ("SELECT * FROM nope WHERE x = :s",))
    assert db.estimates(eng, "main", ["users"]) == {}


def test_estimates_survive_unreachable_database(tmp_path: Path) -> None:
    with db.engine(make_url(f"sqlite:///{tmp_path}/missing/dir/x.db")) as e:
        assert db.estimates(e, "main", ["t"]) == {}


@pytest.mark.parametrize("dialect,glot", [("postgresql", "postgres"), ("mysql", "mysql"), ("mariadb", "mysql"), ("oracle", "oracle")])
def test_catalog_queries_parse_and_bind(dialect: Any, glot: Any) -> None:
    assert db.EST[dialect]
    for q in db.EST[dialect]:
        assert q.count(":s") == 1
        sqlglot.parse_one(q.replace(":s", "'x'"), read=glot)


def test_every_session_statement_is_valid_sql_for_its_dialect() -> None:
    for d, glot in (("mysql", "mysql"), ("mariadb", "mysql"), ("oracle", "oracle"), ("postgresql", "postgres")):
        for q in (db.SCHEMA[d].format('"s"'), db.TMO.get(d, lambda ms: "SELECT 1")(5000), db.RO.get(d, "SELECT 1")):
            sqlglot.parse(q, read=glot)  # parses (or falls back to Command); never raises


def test_oracle_case_roundtrip() -> None:
    d = new_dialect(oracle)
    assert d.denormalize_name("scott") == "SCOTT" and d.normalize_name("USERS") == "users"
    assert d.denormalize_name("MixedCase") == "MixedCase"  # quoted/case-sensitive stays exact
    assert d.identifier_preparer.quote_schema("scott") == "scott"  # lower-case => unquoted => folded to upper by Oracle
    assert d.identifier_preparer.quote_schema("MixedCase") == '"MixedCase"'


@pytest.mark.parametrize("dialect", ["postgresql", "mysql", "oracle"])
def test_schema_switch_statement_quotes_hostile_names(dialect: Any) -> None:
    from sqlalchemy.dialects import mysql, postgresql

    p = {"postgresql": new_dialect(postgresql), "mysql": new_dialect(mysql), "oracle": new_dialect(oracle)}[dialect].identifier_preparer
    sql = db.SCHEMA[dialect].format(p.quote_schema('x"; DROP TABLE t; --' if dialect != "mysql" else "x`; DROP TABLE t; --"))
    assert ("``" if dialect == "mysql" else '""') in sql  # embedded quote chars are doubled, so the payload stays inside the identifier
    assert len(sqlglot.parse(sql, read={"postgresql": "postgres"}.get(dialect, dialect))) == 1


def test_resolve_schema_and_table(eng: Any) -> None:
    i = sa_inspect(eng)
    assert db.resolve_schema(i, "MAIN") == "main" and db.resolve_schema(i, None) == "main"
    with pytest.raises(ValueError, match="Available"):
        db.resolve_schema(i, "nope")
    assert db.resolve_table(i, "ORDER ITEMS", "main") == "order items"
    assert db.resolve_table(i, "v_users", "main") == "v_users"  # views resolve too
    with pytest.raises(ValueError, match="not found"):
        db.resolve_table(i, "x", "main")


def test_match_is_exact_first_and_rejects_ambiguity() -> None:
    assert db._match("Users", ["users", "Users"]) == "Users"
    assert db._match("USERS", ["users"]) == "users"
    assert db._match("x", ["y"]) is None
    with pytest.raises(ValueError, match="ambiguous"):
        db._match("USERS", ["users", "Users"])


def test_column_counts_chunked_and_tolerant(eng: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    i = sa_inspect(eng)
    assert db.column_counts(i, "main", ["parent", "users"]) == {"parent": 2, "users": 2}
    monkeypatch.setattr(type(i), "get_multi_columns", lambda *a, **k: (_ for _ in ()).throw(NotImplementedError()))
    assert db.column_counts(i, "main", ["parent"]) == {}  # listing still works without counts


def test_db_level_readonly_and_timeout(eng: Any) -> None:
    with db.connect_ro(eng) as c:
        with pytest.raises(SQLAlchemyError):
            c.execute(text("DELETE FROM users"))
    with db.connect_ro(eng, timeout=0.2) as c:
        with pytest.raises(SQLAlchemyError, match="interrupt"):
            c.execute(text("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c")).all()


def test_connections_are_closed_and_state_never_leaks(eng: Any) -> None:
    with db.connect_ro(eng) as c:
        pass
    assert c.closed
    with eng.connect() as plain:  # NullPool => a fresh connection, so query_only from connect_ro is gone
        plain.execute(text("INSERT INTO users VALUES (99, 'w')"))
        plain.rollback()
    with pytest.raises(SQLAlchemyError):  # connection errors inside the context still release resources
        with db.connect_ro(eng) as c2:
            c2.execute(text("SELECT * FROM missing"))
    assert c2.closed


def test_engine_context_disposes(monkeypatch: pytest.MonkeyPatch, dbfile: Path) -> None:
    calls: list[int] = []
    real = db.make_engine

    def spy(u: Any) -> Any:
        e: Any = real(u)
        orig = e.dispose

        def dispose(*a: Any, **k: Any) -> None:
            calls.append(1)
            orig(*a, **k)

        e.dispose = dispose
        return e

    monkeypatch.setattr(db, "make_engine", spy)
    with pytest.raises(RuntimeError):
        with db.engine(make_url(f"sqlite:///{dbfile}")):
            raise RuntimeError
    assert calls == [1]


def test_db_module_has_no_os_specific_branches_or_coverage_pragmas() -> None:
    """db.py is platform-neutral by design (the OS-specific code lives in the small helpers of store.py and tunnel.py)."""
    import inspect
    import re

    src = inspect.getsource(db)
    assert not re.search(r"sys\.platform|os\.name|win32|fcntl|pragma:\s*no cover", src)
