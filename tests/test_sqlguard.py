from typing import Any

import pytest

from db_lens.sqlguard import MAX_LEN, UnsafeSQL, validate

SAFE = [
    ("sqlite", s)
    for s in [
        "select 1",
        "SELECT * FROM t WHERE a='; DROP TABLE x'",
        "with x as (select 1) select * from x",
        "select 1 -- drop table t",
        "select 1 union select 2",
        "select update_time, replace(a,'b','c') from t;",
        "/* DELETE */ select 1",
        "select lo_price, sleep, dbms_x from t",
        "values (1),(2)",
        "with recursive c(x) as (select 1 union all select x+1 from c where x<5) select * from c",
        "pragma table_info(users)",
        "pragma table_info('users')",
        "pragma main.index_list(users)",
        "PRAGMA database_list",
        "pragma page_count",
        "pragma foreign_key_check",
        "select name from sqlite_master where type='table'",
    ]
] + [
    ("postgresql", "explain select 1"),
    ("postgresql", "show search_path"),
    ("postgresql", "select pg_size_pretty(1)"),
    ("postgresql", "select * from a join b using (id) where x in (select y from z)"),
    ("mysql", "show tables"),
    ("mysql", "describe t"),
    ("mysql", "show create table t"),
    ("oracle", "select * from dual"),
    ("oracle", "select dbms_metadata.get_ddl('TABLE','T') from dual"),
    ("oracle", "select * from table(dbms_xplan.display())"),
    ("mariadb", "select 1"),
    ("", "select 1"),
]

UNSAFE = [
    ("sqlite", s)
    for s in [
        "delete from t",
        "select 1; select 2",
        "insert into t values (1)",
        "drop table t",
        "create table x(a int)",
        "update t set a=1",
        "attach database 'x' as y",
        "",
        "   ;  ",
        "select (",
        "begin; drop table t",
        "vacuum",
        "reindex",
        "savepoint a",
        "select 1; -- x\n drop table t",
        "select 1 /* ; */ ; drop table t",
        "with a as (select 1) insert into t select * from a",
        "with a as (select 1) delete from t",
        "with a as (select 1) update t set x=1",
        "create table t2 as select * from t",
        "select load_extension('x')",
        "pragma writable_schema=1",
        "pragma foreign_keys=off",
        "pragma user_version=5",
        "pragma journal_mode=wal",
        "pragma wal_checkpoint(truncate)",
        "pragma",
        "select \x00 1",
        "select 1" + " " * MAX_LEN,
        "select " + "(" * 4000 + "1" + ")" * 4000,
        "explain explain analyze delete from t",
    ]
] + [
    ("postgresql", "with x as (delete from t returning *) select * from x"),
    ("postgresql", "with x as (insert into t values (1) returning *) select * from x"),
    ("postgresql", "with x as (update t set a=1 returning *) select * from x"),
    ("postgresql", "select * into t2 from t"),
    ("postgresql", "explain analyze delete from t"),
    ("postgresql", "explain (analyze) select 1"),
    ("postgresql", "explain"),
    ("postgresql", "select pg_sleep(5)"),
    ("postgresql", "select pg_catalog.pg_sleep(5)"),
    ("postgresql", "select pg_advisory_lock(1)"),
    ("postgresql", "select * from t for update"),
    ("postgresql", "lock table t"),
    ("postgresql", "truncate t"),
    ("postgresql", "do $$ begin delete from t; end $$"),
    ("postgresql", "call p()"),
    ("postgresql", "copy t to '/tmp/x'"),
    ("postgresql", "select lo_import('/etc/passwd')"),
    ("postgresql", "set role admin"),
    ("postgresql", "grant all on t to u"),
    ("postgresql", "select * from dblink('a','b') as t(x int)"),
    ("postgresql", "select nextval('s')"),
    ("postgresql", "pragma table_info(t)"),
    ("postgresql", "commit"),
    ("mysql", "select 1 /*! drop table t */"),
    ("mysql", "select 1 /*!50000 drop table t */"),
    ("mariadb", "select 1 /*M! drop table t */"),
    ("mysql", "select load_file('/etc/passwd')"),
    ("mysql", "select * into outfile '/tmp/x' from t"),
    ("mysql", "select 1 into @v"),
    ("mysql", "select get_lock('a',1)"),
    ("mysql", "replace into t values (1)"),
    ("mysql", "use other"),
    ("mysql", "select sleep(10)"),
    ("oracle", "begin null; end;"),
    ("oracle", "select dbms_lock.sleep(5) from dual"),
    ("oracle", "select dbms_output.put_line('x') from dual"),
    ("oracle", "select utl_http.request('x') from dual"),
    ("oracle", "explain plan for select 1 from dual"),
    ("oracle", "merge into t using s on (1=1) when matched then update set a=1"),
]


@pytest.mark.parametrize("dialect,sql", SAFE)
def test_safe(dialect: Any, sql: Any) -> None:
    validate(sql, dialect)


@pytest.mark.parametrize("dialect,sql", UNSAFE, ids=[f"{d}:{s[:40]!r}" for d, s in UNSAFE])
def test_unsafe(dialect: Any, sql: Any) -> None:
    with pytest.raises(UnsafeSQL):
        validate(sql, dialect)


def test_returns_ast_for_stream_decision() -> None:
    from sqlglot import exp

    assert isinstance(validate("select 1", "postgresql"), exp.Query)
    assert not isinstance(validate("show tables", "mysql"), exp.Query)


def test_error_never_echoes_whole_statement() -> None:
    with pytest.raises(UnsafeSQL) as e:
        validate("select ((( 'hunter2-secret-token'", "sqlite")
    assert "hunter2" not in str(e.value)
