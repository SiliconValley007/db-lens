"""AST-based read-only SQL validation. Fail-closed: anything unparseable or unrecognised is rejected.
Dialect rules cover SQLite, PostgreSQL, MySQL/MariaDB and Oracle; the Oracle rules are tested on parsed statements only (no
live Oracle server was available), the others are also exercised through live sessions in tests/test_integration.py.

BOUNDARY: this is a client-side convenience, not a security control. It cannot see inside user-defined functions or stored
procedures (``SELECT run_mutation_job()`` parses as a harmless SELECT). Real write protection = a dedicated read-only
database user/role + the engine-level read-only session db-lens enables and verifies. See NOTICE."""

from __future__ import annotations

import logging
import re
from typing import Any, cast

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.ERROR)

MAX_LEN = 1_000_000
DIALECTS = {"postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "oracle": "oracle", "sqlite": "sqlite"}
ALLOWED_ROOTS = tuple(getattr(exp, n) for n in ("Query", "Show", "Describe", "Values") if hasattr(exp, n))
MUTATING = tuple(
    getattr(exp, n)
    for n in (
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Create",
        "Drop",
        "Alter",
        "Command",
        "Copy",
        "TruncateTable",
        "Into",
        "Pragma",
        "Set",
        "Use",
        "Grant",
        "Revoke",
        "Transaction",
        "Commit",
        "Rollback",
        "LoadData",
        "Kill",
        "Analyze",
        "Attach",
        "Detach",
        "Refresh",
        "Lock",
    )
    if hasattr(exp, n)
)
# PRAGMAs that cannot change state whatever their argument (setters such as user_version/journal_mode are excluded).
SAFE_PRAGMAS = frozenset(
    {
        "table_info",
        "table_xinfo",
        "table_list",
        "index_list",
        "index_info",
        "index_xinfo",
        "foreign_key_list",
        "database_list",
        "collation_list",
        "function_list",
        "module_list",
        "pragma_list",
        "compile_options",
        "page_count",
        "freelist_count",
        "data_version",
        "quick_check",
        "integrity_check",
        "foreign_key_check",
    }
)
SAFE_COMMANDS = frozenset({"SHOW", "DESCRIBE", "DESC", "EXPLAIN"})
DENY_FUNCS = frozenset(
    {
        "pg_sleep",
        "sleep",
        "benchmark",
        "set_config",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_notify",
        "load_file",
        "load_extension",
        "nextval",
        "setval",
        "get_lock",
        "release_lock",
        "xp_cmdshell",
        "sys_exec",
        "sys_eval",
    }
)
DENY_PREFIX = (
    "dbms_",
    "utl_",
    "pg_read_",
    "pg_ls_",
    "pg_advisory",
    "pg_logical_",
    "pg_replication_",
    "pg_switch_",
    "pg_create_",
    "pg_drop_",
    "pg_file_",
    "lo_",
    "dblink",
)
SAFE_PREFIX = ("dbms_xplan", "dbms_metadata")  # read-only Oracle helpers
EXECUTABLE_COMMENT = re.compile(r"/\*[mM]?!")  # MySQL /*! ... */ and MariaDB /*M! ... */ run their contents


NOTICE = (
    "SQL checks are best-effort: UDFs and stored procedures can write and are invisible to the parser. "
    "Connect with a dedicated read-only database role; db-lens also enables and verifies a read-only session."
)


class UnsafeSQL(ValueError):
    """Raised when a statement cannot be proven read-only."""


# Anonymous calls that are built-ins sqlglot merely does not model; everything else Anonymous is *probably* user-defined.
KNOWN_BUILTINS = frozenset(
    {
        "pg_size_pretty",
        "pg_typeof",
        "pg_table_size",
        "pg_total_relation_size",
        "pg_relation_size",
        "pg_get_viewdef",
        "pg_get_indexdef",
        "pg_get_constraintdef",
        "version",
        "current_schema",
        "current_database",
        "has_table_privilege",
        "has_schema_privilege",
        "has_function_privilege",
        "pg_has_role",
        "to_regclass",
        "generate_series",
        "unnest",
        "json_each",
        "json_extract",
        "json_valid",
        "typeof",
        "sqlite_version",
        "database",
        "schema",
        "user",
        "current_user",
        "session_user",
        "sys_context",
        "regexp_replace",
        "table",
    }
)


def classify_calls(root: exp.Expression, custom: set[str] | frozenset[str] = frozenset()) -> tuple[list[str], list[str]]:
    """(calls to routines known to be user-created in this database, calls to functions the parser does not recognise).
    Both may write and neither body is visible here; the first is certain (the account probe listed them), the second is a
    hint (it also contains built-ins sqlglot merely does not model)."""
    known = {n.lower() for n in custom}
    hits: dict[str, None] = {}
    unknown: dict[str, None] = {}
    for node in root.find_all(exp.Anonymous):
        n = node.name.lower()
        parent = node.parent
        safe_pkg = isinstance(parent, exp.Dot) and any(i.name.lower().startswith(SAFE_PREFIX) for i in parent.this.find_all(exp.Identifier))
        if n in known:
            hits[node.name] = None
        elif n not in KNOWN_BUILTINS and not n.startswith(SAFE_PREFIX) and not safe_pkg:
            unknown[node.name] = None
    return list(hits), list(unknown)


def _denied(name: str) -> bool:
    name = name.lower()
    return name in DENY_FUNCS or (name.startswith(DENY_PREFIX) and not name.startswith(SAFE_PREFIX))


def _pragma(root: exp.Expression, dialect: str | None) -> exp.Expression:
    if dialect not in (None, "sqlite"):
        raise UnsafeSQL("PRAGMA is only available on SQLite.")
    node = root.this
    node = node.this if isinstance(node, exp.EQ) else node  # `pragma x(arg)` and `pragma x = arg` share one shape
    node = node.expression if isinstance(node, exp.Dot) else node  # schema-qualified: main.table_info(t)
    name = str(getattr(node, "name", "")).lower()
    if name not in SAFE_PRAGMAS:
        raise UnsafeSQL(f"PRAGMA {name or '?'} is not allowed. Read-only pragmas: {', '.join(sorted(SAFE_PRAGMAS))}.")
    return root


def _command(root: exp.Command, dialect: str) -> exp.Expression:
    word = str(root.this).upper()
    if word not in SAFE_COMMANDS:
        raise UnsafeSQL(f"{word} statements are not allowed.")
    rest = root.expression.name.strip() if root.expression else ""
    if word == "EXPLAIN":  # ANALYZE executes the statement; Oracle's EXPLAIN PLAN writes to PLAN_TABLE
        first = rest.split(None, 1)[0].upper() if rest else ""
        if not rest or rest[0] == "(" or first in {"ANALYZE", "ANALYSE", "PLAN"}:
            raise UnsafeSQL("EXPLAIN options, ANALYZE and EXPLAIN PLAN are not allowed.")
        validate(rest, dialect)
    return root


def validate(sql: str, dialect: str = "") -> exp.Expression:
    """Return the parsed single read-only statement, or raise UnsafeSQL."""
    if len(sql) > MAX_LEN or "\x00" in sql:
        raise UnsafeSQL("Statement is too large or contains NUL bytes.")
    d = DIALECTS.get(dialect)
    if d == "mysql" and EXECUTABLE_COMMENT.search(sql):
        raise UnsafeSQL("MySQL/MariaDB executable comments (/*! ... */) are not allowed.")
    try:
        stmts = [s for s in sqlglot.parse(sql, read=d) if s]
    except Exception as e:  # noqa: BLE001 - includes RecursionError: fail closed on any parser problem
        raise UnsafeSQL(f"Cannot verify statement is read-only: {(str(e).splitlines() or [type(e).__name__])[0][:150]}") from e
    if len(stmts) != 1:
        raise UnsafeSQL("Exactly one statement is allowed." if stmts else "Empty statement.")
    root: Any = stmts[0]
    if isinstance(root, exp.Pragma):
        return _pragma(root, d)
    if isinstance(root, exp.Command):
        return _command(root, dialect)
    if not isinstance(root, ALLOWED_ROOTS):
        raise UnsafeSQL(f"{type(root).__name__} statements are not allowed.")
    for node in root.walk():  # also catches writes nested in CTEs/subqueries (WITH x AS (DELETE ... RETURNING ...))
        if isinstance(node, MUTATING):
            raise UnsafeSQL(f"{type(node).__name__} is not allowed in read-only mode.")
        if isinstance(node, exp.Func):
            name = node.name if isinstance(node, exp.Anonymous) else cast(Any, node).sql_name()
            if _denied(name):
                raise UnsafeSQL(f"Function {name!r} is not allowed in read-only mode.")
        elif isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):  # pkg.func(): check the qualifier
            for ident in node.this.find_all(exp.Identifier):
                if _denied(ident.name):
                    raise UnsafeSQL(f"Package {ident.name!r} is not allowed in read-only mode.")
    return cast(exp.Expression, root)
