import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest
import typer
from sqlalchemy import make_url

from db_lens import cli, db, store


def test_connect_profiles_and_default_switching(home: Path, dbfile: Path, runner: Any, tmp_path: Path) -> None:
    runner("connect", f"sqlite:///{dbfile}", "-n", "a", has=["Connected", "secret: none"])
    other = tmp_path / "other.db"
    import sqlite3

    sqlite3.connect(other).executescript("CREATE TABLE only_here(x INT);")
    runner("connect", f"sqlite:///{other}", "-n", "b")
    runner("tables", has="only_here", hasnt="users")  # b is now default
    runner("tables", "-p", "a", has="users", hasnt="only_here")  # explicit profile wins
    runner("connect", f"sqlite:///{dbfile}", "-n", "b")  # overwrite b
    runner("tables", has="users")
    runner("profiles", has=["a", "b", "★"])


def test_connect_validates_schema_and_saves_it(home: Path, dbfile: Path, runner: Any) -> None:
    runner("connect", f"sqlite:///{dbfile}", "-S", "nope", code=1, has="Available")
    assert store.load()["profiles"] == {}  # nothing saved on failure
    runner("connect", f"sqlite:///{dbfile}", "-n", "s", "-S", "MAIN")
    runner("tables", has="users")
    assert store.load()["profiles"]["s"]["schema"] == "main"


def test_connection_failures_are_panels_without_leaking_credentials(home: Path, runner: Any, tmp_path: Path) -> None:
    runner("connect", f"sqlite:///{tmp_path}/no/such/dir/x.db", code=1, has="OperationalError")
    runner("connect", "not a url at all", code=1, has="ArgumentError")
    runner("connect", "postgresql://bob:hunter2pw@127.0.0.1:1/d", code=1)  # no driver / refused: either way a panel
    assert "hunter2pw" not in runner.last.output
    runner("connect", "postgresql://bob:${DEFINITELY_UNSET_VAR}@h/d", code=1, has="DEFINITELY_UNSET_VAR")


def test_secret_never_printed_even_if_driver_echoes_it(home: Path, runner: Any, monkeypatch: pytest.MonkeyPatch, dbfile: Path) -> None:
    monkeypatch.setenv("PW", "echo-me-9876")
    monkeypatch.setattr(db, "make_engine", lambda u: (_ for _ in ()).throw(RuntimeError(f"bad dsn {u.render_as_string(hide_password=False)}")))
    runner("connect", "postgresql://u:${PW}@h/d", code=1, has="RuntimeError", hasnt="echo-me-9876")


def test_schema_reflection_columns_keys_indexes_constraints(prof: Path, runner: Any) -> None:
    out = runner("inspect", "order items", has=["unit price", "NOT NULL", "→ parent.a", "→ parent.b", "UNIQUE", "ix_note", "CHECK", "Indexes"])
    runner("inspect", "PARENT", has=["PK", "a", "b"])
    runner("inspect", "v_users", has=["id", "name"])
    assert out.count("PK") == 1  # single-column PK 'id' on order items only
    runner("inspect", "nope", code=1, has="not found")
    runner("inspect", "users", "-S", "nope", code=1, has="Available")


def test_identifiers_with_quotes_and_markup_are_safe_everywhere(prof: Path, runner: Any) -> None:
    weird = 'we"ird [red]name'
    runner("tables", has=["[red]name", "ird"])
    runner("inspect", weird, "-n", "5", has=["co", "v1", "[bold]c2"])
    assert json.loads(runner("query", 'SELECT "co""l" AS c FROM "we""ird [red]name"', "-o", "json"))[0] == {"c": "v1"}


def test_tables_metrics_search_and_empty(prof: Path, runner: Any) -> None:
    out = runner("tables", has=["order items", "Rows", "pages"])
    assert "?" not in out.split("pages")[0]  # exact counts + dbstat sizes were found
    runner("tables", "-s", "USER", has="users", hasnt="parent")
    runner("tables", "-s", "zzz", has="No tables found")
    runner("tables", "-S", "main", "--no-pager", has="users")


def test_tables_degrade_gracefully_when_metrics_unavailable(prof: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "estimates", lambda *a, **k: {})
    monkeypatch.setattr(db, "column_counts", lambda *a, **k: {})
    runner("tables", has=["users", "?"])  # still lists everything


def test_query_formats_limit_stdin_file(prof: Path, runner: Any, tmp_path: Path) -> None:
    rows = json.loads(runner("query", "SELECT id, name FROM users ORDER BY id", "-o", "json"))
    assert rows[2] == {"id": 3, "name": None} and rows[4]["name"] == "NULL"
    parsed = list(csv.reader(io.StringIO(runner("query", "SELECT id, name FROM users WHERE id <= 3", "-o", "csv"))))
    assert parsed[0] == ["id", "name"] and len(parsed) == 4
    runner("query", "SELECT * FROM users", "-l", "2", has="truncated")
    runner("query", "-", inp="SELECT name FROM users WHERE id=1;", has=["a", "ms"])
    runner("query", inp="SELECT 1 AS piped", has="piped")
    f = tmp_path / "q.sql"
    f.write_text("SELECT 1 AS one;\n")
    runner("query", "-f", str(f), has="one")
    runner("query", "SELECT 1", "-l", "0", code=2)  # usage error from the CLI layer, not a traceback


def test_metadata_goes_to_stderr_for_machine_formats(prof: Path, runner: Any) -> None:
    out = runner("query", "SELECT 1 AS x", "-o", "json")
    assert json.loads(out) == [{"x": 1}]  # stdout is pure JSON; "1 row(s) · N ms" is on stderr


def test_wide_values_and_names_null_and_empty(prof: Path, runner: Any) -> None:
    long_name = "c" * 150
    out = runner("query", f"SELECT '{'x' * 200}' AS {long_name}, NULL AS n, '' AS e, 'NULL' AS s", "--max-cell", "20", "-l", "5")
    assert out.count("c") == 150  # the whole column name is shown (folded over lines)
    assert out.count("…") == 1 and "x" * 21 not in out  # only the long cell value is cut, at --max-cell
    assert out.count("NULL") == 2  # real NULL and the literal string 'NULL' both visible


def test_terminal_escape_and_markup_injection_from_data_is_neutralised(prof: Path, runner: Any) -> None:
    out = runner("query", "SELECT name FROM users WHERE id = 4")
    assert "\x1b" not in out and "\x07" not in out and "\\x1b" in out
    out = runner("inspect", "order items", "-n", "1")
    assert "[red]x[/red]" in out  # printed literally, not interpreted as styling


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM users",
        "SELECT 1; DROP TABLE users",
        "PRAGMA writable_schema=1",
        "PRAGMA user_version=3",
        "WITH a AS (SELECT 1) DELETE FROM users",
        "INSERT INTO users VALUES (9,'z')",
        "SELEC x",
        "",
        "SELECT load_extension('x')",
    ],
)
def test_rejected_statements_leave_data_intact(prof: Path, runner: Any, sql: Any) -> None:
    runner("query", sql, code=1, has="UnsafeSQL" if sql else "Empty")
    assert json.loads(runner("query", "SELECT count(*) AS n FROM users", "-o", "json")) == [{"n": 6}]


def test_pragma_reads_are_supported(prof: Path, runner: Any) -> None:
    runner("query", "PRAGMA table_info(users)", has=["name", "INTEGER"])
    runner("query", "PRAGMA index_list('order items')", has="ix_note")
    runner("query", "PRAGMA page_count", has="page_count")


def test_query_with_schema_and_bad_schema(prof: Path, runner: Any) -> None:
    runner("query", "SELECT 1 AS ok", "-S", "main", has="ok")
    runner("query", "SELECT 1", "-S", "nope", code=1, has="Available")


def test_timeout_and_missing_profile(prof: Path, runner: Any) -> None:
    runner("query", "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c", "-t", "0.3", code=1, has="Timed out")
    runner("tables", "-p", "ghost", code=1, has="not found")
    runner("query", "SELECT 1", "-p", "ghost", code=1, has="not found")


def test_query_with_empty_stdin_is_a_clear_error(prof: Path, runner: Any) -> None:
    runner("query", "-", inp="", code=1, has="Empty")


def test_corrupt_config_yields_panel_for_every_command(home: Path, runner: Any) -> None:
    store.CONFIG.parent.mkdir(parents=True)
    store.CONFIG.write_text("{broken")
    for cmd in (["profiles"], ["tables"], ["inspect", "t"], ["query", "SELECT 1"], ["connect", "sqlite://"]):
        runner(*cmd, code=1, has="corrupt")
    assert store.CONFIG.read_text() == "{broken"


def test_guard_maps_unexpected_errors_and_hints(capsys: pytest.CaptureFixture[str]) -> None:
    class Lost(Exception):
        connection_invalidated = True

    for exc, needle in ((Lost("gone"), "Connection lost"), (RuntimeError("statement timeout hit"), "Timed out"), (RuntimeError("plain"), "plain")):
        with pytest.raises(typer.Exit) as e:
            with cli.guard():
                raise exc
        assert e.value.exit_code == 1
        assert needle in capsys.readouterr().err
    exits: list[tuple[BaseException, int]] = [(BrokenPipeError(), 0), (KeyboardInterrupt(), 130)]
    for exc2, code in exits:
        with pytest.raises(typer.Exit) as e:
            with cli.guard():
                raise exc2
        assert e.value.exit_code == code


def test_clean_and_human_helpers() -> None:
    assert cli.clean("a\x1b[0m\tb\nc\x00") == "a\\x1b[0m\tb\nc\\x00"
    assert [cli.human(n) for n in (5, 2048, 5 * 1024**2, 3 * 1024**3, 2 * 1024**4)] == ["5 B", "2 KB", "5 MB", "3 GB", "2.0 TB"]


def test_engines_are_disposed_after_every_command(prof: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
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
    runner("tables")
    runner("inspect", "users", "-n", "1")
    runner("query", "SELECT 1")
    runner("query", "DELETE FROM users", code=1)
    assert len(calls) == 4


def test_module_entrypoint_and_url_roundtrip(prof: Path) -> None:
    import runpy
    import sys

    sys.argv = ["db-lens", "--help"]
    with pytest.raises(SystemExit) as e:
        runpy.run_module("db_lens", run_name="__main__")
    assert e.value.code == 0
    assert make_url("sqlite:///x.db").get_backend_name() == "sqlite"


def test_csv_uses_lf_line_endings_so_windows_text_mode_does_not_produce_cr_cr_lf(prof: Path, runner: Any) -> None:
    out = runner("query", "SELECT id FROM users WHERE id <= 2 ORDER BY id", "-o", "csv")
    assert out == "id\n1\n2\n" and "\r" not in out


def test_unnamed_constraints_are_not_shown_as_none(prof: Path, runner: Any) -> None:
    out = runner("inspect", "order items")
    assert "None" not in out and "UNIQUE (sku)" in out and "CHECK" in out


def test_version_flag(runner: Any) -> None:
    from db_lens import __version__

    runner("--version", has=f"db-lens {__version__}")


def test_profiles_use_remove_and_confirmation(home: Path, dbfile: Path, runner: Any) -> None:
    for n in ("a", "b", "c"):
        runner("connect", f"sqlite:///{dbfile}", "-n", n)
    runner("profiles", has=["a", "b", "c ★", "TLS"])
    runner("profiles", "list", has="c ★")
    runner("profiles", "use", "a", has="Default profile is now a")
    runner("profiles", "use", "zzz", code=1, has="not found (saved: a, b, c)")
    runner("profiles", "remove", "b", code=1, inp="n\n", has="Aborted")  # declined: nothing happens
    assert set(store.load()["profiles"]) == {"a", "b", "c"}
    runner("profiles", "remove", "b", inp="y\n", has="Removed b")
    runner("profiles", "remove", "zzz", "--yes", code=1, has="not found")
    runner("profiles", "remove", "a", "-y", has=["Removed a", "Default profile is now c"])  # one left: becomes default
    runner("profiles", "remove", "c", "-y", has="Removed c")
    runner("profiles", has="Profile")  # empty table, no crash
    runner("tables", code=1, has="Profile None not found")


def test_removing_the_default_with_several_left_says_so(home: Path, dbfile: Path, runner: Any) -> None:
    for n in ("a", "b", "c"):
        runner("connect", f"sqlite:///{dbfile}", "-n", n)
    runner("profiles", "remove", "c", "-y", has="No default profile")
    runner("tables", code=1, has="profiles use")
    runner("tables", "-p", "a", has="users")


def test_connect_from_structured_options_needs_no_uri_or_quoting(home: Path, dbfile: Path, runner: Any) -> None:
    runner("connect", "-D", "sqlite", "-d", str(dbfile), "-n", "s", has="Connected s")
    runner("tables", has="users")
    runner("connect", "-D", "sqlite", "-d", str(dbfile), "--option", "timeout=5", "-n", "s2", has="Connected s2")
    assert "timeout=5" in store.load()["profiles"]["s2"]["uri"]


def test_connect_uri_sources(home: Path, dbfile: Path, runner: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    uri = f"sqlite:///{dbfile}"
    monkeypatch.setenv("DBL_URI", uri)
    runner("connect", "--uri-env", "DBL_URI", "-n", "e", has="Connected e")
    f = tmp_path / "uri.txt"
    f.write_text(f"\n{uri}\nignored\n", encoding="utf-8")
    runner("connect", "--uri-file", str(f), "-n", "f", has="Connected f")
    runner("connect", "-", "-n", "i", inp=uri + "\n", has="Connected i")
    runner("connect", "--uri-env", "NOPE_NOT_SET", code=1, has="NOPE_NOT_SET is not set")
    runner("connect", uri, "--uri-env", "DBL_URI", code=1, has="only one of")
    runner("connect", uri, "-D", "sqlite", code=1, has="not both")
    runner("connect", code=1, has="Give a connection URI")
    (tmp_path / "empty.txt").write_text("\n")
    runner("connect", "--uri-file", str(tmp_path / "empty.txt"), code=1, has="is empty")


def test_password_inputs_and_no_leaks(home: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    pg = ["-D", "postgresql", "--host", "127.0.0.1", "--port", "1", "-u", "bob", "-d", "x"]
    runner("connect", *pg, "--ask-password", inp="Sup3r$ecret!\n", code=1, hasnt="Sup3r$ecret!")
    runner("connect", *pg, "--password-stdin", inp="Sup3r$ecret!\n", code=1, hasnt="Sup3r$ecret!")
    runner("connect", *pg, "--password-stdin", inp="\n", code=1, has="empty line")
    runner("connect", *pg, "--password-env", "NOT A NAME", code=1, has="variable NAME")
    runner("connect", *pg, "--password-env", "DBL_UNSET_PW", code=1, has="DBL_UNSET_PW is not set")
    runner("connect", *pg, "--ask-password", "--password-stdin", code=1, has="only one of")
    runner("connect", "-", *pg[:0], "--password-stdin", inp="x\n", code=1, has="cannot both be read from stdin")
    runner("connect", *pg, "--option", "password=hunter2", code=1, has="looks like a secret")
    assert store.load()["profiles"] == {}


def test_ssl_flags_rejected_where_they_cannot_work(home: Path, dbfile: Path, runner: Any) -> None:
    runner("connect", "-D", "sqlite", "-d", str(dbfile), "--ssl-mode", "require", code=1, has="not supported for sqlite")
    runner("connect", "-D", "mysql", "--host", "h", "--ssl-mode", "verify-full", code=1, has="without --ssl-ca")


def test_password_on_the_command_line_is_called_out(home: Path, runner: Any) -> None:
    runner("connect", "postgresql://u:pw123456@127.0.0.1:1/x", code=1)  # fails to connect; hint only after success
    assert "pw123456" not in runner.last.output


def test_tls_demand_is_enforced_on_every_command(prof: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = store.load()
    cfg["profiles"]["shop"]["tls"] = "require"
    store.save(cfg)
    runner("tables", code=1, has="TlsError")  # fail closed when unverifiable
    monkeypatch.setattr(db, "tls_in_use", lambda e: False)
    runner("query", "SELECT 1", code=1, has="TlsError")
    assert "NOT" in " ".join(runner.last.output.split()) and "encrypted" in " ".join(runner.last.output.split())
    monkeypatch.setattr(db, "tls_in_use", lambda e: True)
    runner("query", "SELECT 1 AS x", has="x")
    runner("profiles", has="require")


# ---- profiles rename, json/csv output, verbose, completion, ssh flags ---------------------------------------------------
def test_profiles_rename_command(home: Path, dbfile: Path, runner: Any) -> None:
    runner("connect", f"sqlite:///{dbfile}", "-n", "old")
    runner("connect", f"sqlite:///{dbfile}", "-n", "other")
    runner("profiles", "rename", "old", "new", has="Renamed old to new")
    runner("profiles", has=["new", "other ★"])
    runner("profiles", "rename", "new", "other", code=1, has="already exists")
    runner("profiles", "rename", "zzz", "x", code=1, has="not found (saved: new, other)")
    runner("tables", "-p", "new", has="users")
    runner("tables", "-p", "old", code=1, has="not found")


def test_profiles_json_and_csv(prof: Path, runner: Any) -> None:
    data = json.loads(runner("profiles", "-o", "json"))
    assert data == [
        {
            "name": "shop",
            "default": True,
            "uri": data[0]["uri"],
            "schema": None,
            "secret": "none",
            "tls": None,
            "ssh": None,
            "account": "write-capable",
            "risks": 0,
        }
    ]
    assert "password" not in json.dumps(data).lower() and data[0]["uri"].startswith("sqlite:///")
    assert json.loads(runner("profiles", "list", "--format", "json")) == data
    rows = list(csv.reader(io.StringIO(runner("profiles", "list", "-o", "csv"))))
    assert rows[0][:3] == ["name", "default", "uri"] and rows[1][0] == "shop"
    assert "1 profile(s)" in runner.last.output


def test_profiles_empty_machine_output(home: Path, runner: Any) -> None:
    assert json.loads(runner("profiles", "-o", "json")) == []
    assert runner("profiles", "-o", "csv") == "\n"


def test_tables_json_and_csv(prof: Path, runner: Any) -> None:
    data = json.loads(runner("tables", "-o", "json"))
    by = {r["table"]: r for r in data}
    assert set(by) == {"order items", "parent", 'we"ird [red]name', "users"} or set(by) >= {"order items", "parent", "users"}
    assert by["users"]["columns"] == 2 and by["users"]["rows"] == 6 and by["users"]["rows_estimated"] is False
    assert isinstance(by["users"]["size_bytes"], (int, type(None)))
    assert "table(s)" in runner.last.output and "sqlite" in runner.last.output
    rows = list(csv.reader(io.StringIO(runner("tables", "-o", "csv", "-s", "user"))))
    assert rows[0] == ["table", "columns", "rows", "rows_estimated", "size_bytes"] and rows[1][:3] == ["users", "2", "6"]
    assert json.loads(runner("tables", "-o", "json", "-s", "zzz")) == []  # empty result is valid JSON, not a message


def test_tables_csv_neutralises_formula_table_names(home: Path, tmp_path: Path, runner: Any) -> None:
    import sqlite3

    p = tmp_path / "f.db"
    sqlite3.connect(p).executescript('CREATE TABLE "=cmd|x"(a int); CREATE TABLE "@evil"(b int);')
    runner("connect", f"sqlite:///{p}", "-n", "f")
    out = runner("tables", "-o", "csv")
    assert "'=cmd|x" in out and "'@evil" in out and "\r" not in out


def test_verbose_prints_diagnostics_without_secrets_or_sql(prof: Path, runner: Any) -> None:
    runner("query", "SELECT 'sensitive-literal' AS s", hasnt="[debug]")  # silent by default
    out = runner("-v", "query", "SELECT 'sensitive-literal' AS s")
    text = runner.last.output
    assert "db-lens [debug] profile 'shop' -> sqlite:///" in text and "session: PRAGMA query_only=ON" in text
    assert "read-only check PRAGMA query_only" in text and "statement accepted as read-only (Select)" in text and "fetched 1 row(s)" in text
    assert "sensitive-literal" not in text.split("fetched")[0] and "[debug]" in text and out
    runner("query", "SELECT 1", hasnt="[debug]")  # handler removed again: no bleed-over


def test_verbose_env_var_and_redaction(prof: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_LENS_VERBOSE", "1")
    runner("tables", has="[debug] profile")
    store.remember("TopSecretValue1")
    import logging

    logging.getLogger("db_lens").debug("password is TopSecretValue1")
    runner("tables", hasnt="TopSecretValue1")


def test_completion_of_profile_names(home: Path, dbfile: Path, runner: Any) -> None:
    assert cli.complete_profile("") == []
    for n in ("alpha", "alps", "beta"):
        runner("connect", f"sqlite:///{dbfile}", "-n", n)
    assert cli.complete_profile("al") == ["alpha", "alps"] and cli.complete_profile("be") == ["beta"] and cli.complete_profile("z") == []
    store.CONFIG.write_text("{broken")
    assert cli.complete_profile("") == []  # a broken config completes nothing, never crashes


def test_shell_completion_end_to_end(home: Path, dbfile: Path, runner: Any) -> None:
    import os
    import subprocess
    import sys

    for n in ("alpha", "beta"):
        runner("connect", f"sqlite:///{dbfile}", "-n", n)
    env = {
        **os.environ,
        "DB_LENS_CONFIG": str(store.CONFIG),
        "_DB_LENS_COMPLETE": "complete_bash",
        "COMP_WORDS": "db-lens profiles use al",
        "COMP_CWORD": "3",
        "PYTHONUTF8": "1", "NO_COLOR": "1", "_TYPER_FORCE_DISABLE_TERMINAL": "1",
    }
    p = subprocess.run([sys.executable, "-m", "db_lens"], env=env, capture_output=True, text=True, encoding="utf-8")
    assert "alpha" in p.stdout and "beta" not in p.stdout, p.stdout + p.stderr
    env["COMP_WORDS"], env["COMP_CWORD"] = "db-lens tables -p b", "3"
    assert "beta" in subprocess.run([sys.executable, "-m", "db_lens"], env=env, capture_output=True, text=True, encoding="utf-8").stdout
    env["COMP_WORDS"], env["COMP_CWORD"] = "db-lens pro", "1"
    assert "profiles" in subprocess.run([sys.executable, "-m", "db_lens"], env=env, capture_output=True, text=True, encoding="utf-8").stdout
    assert "--install-completion" in runner("--help")


def test_ssh_flag_validation(home: Path, dbfile: Path, runner: Any) -> None:
    runner("connect", "-D", "sqlite", "-d", str(dbfile), "--ssh", "bastion", code=1, has="SQLite")
    runner("connect", "-D", "postgresql", "--host", "h", "--ssh", "-oProxyCommand=evil", code=1, has="--ssh expects")
    runner("connect", "-D", "postgresql", "--host", "h", "--ssh-key", "k", code=1, has="need --ssh")
    runner("connect", "-D", "postgresql", "--host", "h", "--ssh-accept-new", code=1, has="need --ssh")
    assert store.load()["profiles"] == {}


def test_ssh_profile_goes_through_the_tunnel_on_every_command(prof: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    from db_lens import tunnel

    calls = []

    @contextmanager
    def fake_forward(url: Any, spec: Any, wait: Any = 20.0) -> Any:
        calls.append((url.get_backend_name(), spec))
        yield url

    monkeypatch.setattr(tunnel, "forward", fake_forward)
    cfg = store.load()
    cfg["profiles"]["shop"]["ssh"] = {"host": "me@bastion", "port": 2222, "key": "/k", "accept_new": True}
    store.save(cfg)
    runner("tables", has="users")
    runner("query", "SELECT 1 AS x", has="x")
    assert [c[1] for c in calls] == [tunnel.SshSpec("me@bastion", 2222, "/k", True)] * 2
    runner("profiles", has="bastion")  # (the table wraps cells on an 80-column terminal)
    assert json.loads(runner("profiles", "-o", "json"))[0]["ssh"] == "me@bastion:2222"
    runner("-v", "tables", has="via ssh me@bastion:2222")


def test_tunnel_failure_is_a_panel_not_a_traceback(prof: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from db_lens import tunnel

    def broken(url: Any, spec: Any, wait: Any = 20.0) -> Any:
        raise tunnel.TunnelError("ssh exited with code 255: Permission denied (publickey).\n→ The SSH server rejected the key.")

    monkeypatch.setattr(tunnel, "forward", broken)
    cfg = store.load()
    cfg["profiles"]["shop"]["ssh"] = {"host": "me@bastion"}
    store.save(cfg)
    runner("tables", code=1, has=["TunnelError", "rejected the key"])


# ---- `db-lens gui` and entry-point parity -----------------------------------------------------------------------------
def test_gui_command_opens_the_window_for_a_profile(home: Path, dbfile: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from db_lens.gui import app as gui_app

    runner("connect", f"sqlite:///{dbfile}", "-n", "shop")
    opened: list[str | None] = []
    monkeypatch.setattr(gui_app, "main", lambda profile=None, root=None: opened.append(profile))
    runner("gui")
    runner("gui", "-p", "shop")
    assert opened == [None, "shop"]


def test_gui_command_rejects_unknown_profiles_and_missing_tk(home: Path, dbfile: Path, runner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    runner("connect", f"sqlite:///{dbfile}", "-n", "shop")
    runner("gui", "-p", "nope", code=1, has="not found")
    from tkinter import TclError

    from db_lens import gui as gui_pkg
    from db_lens.gui import app as gui_app

    def no_display(profile: str | None = None, root: Any = None) -> None:
        raise TclError("no display name and no $DISPLAY environment variable")

    monkeypatch.setattr(gui_app, "main", no_display)
    runner("gui", code=1, has="Cannot open the window")
    monkeypatch.delattr(gui_pkg, "app")
    monkeypatch.setitem(__import__("sys").modules, "db_lens.gui.app", None)  # what a Python built without Tk looks like
    runner("gui", code=1, has="needs Tk")


def test_gui_is_listed_in_help(runner: Any) -> None:
    runner("--help", has="gui")


def _help(cmd: list[str]) -> tuple[int, str]:
    import os
    import subprocess

    env = {**os.environ, "COLUMNS": "100", "NO_COLOR": "1", "TERM": "dumb"}
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60, check=False)
    return p.returncode, p.stdout


def test_python_dash_m_and_the_console_script_print_identical_help() -> None:
    import shutil
    import sys

    shim = "import sys; sys.argv[0] = 'db-lens'; from db_lens.cli import run; run()"  # what the generated console script does
    code_m, out_m = _help([sys.executable, "-m", "db_lens", "--help"])
    code_s, out_s = _help([sys.executable, "-c", shim, "--help"])
    assert (code_m, code_s) == (0, 0) and out_m == out_s and "Usage: db-lens" in out_m and "python -m" not in out_m and "__main__" not in out_m
    script = shutil.which("db-lens")
    if script:  # the real installed script, when the package is installed with its entry point
        assert _help([script, "--help"]) == (0, out_m)
    for sub in ("gui", "query"):
        assert _help([sys.executable, "-m", "db_lens", sub, "--help"])[1] == _help([sys.executable, "-c", shim, sub, "--help"])[1]
