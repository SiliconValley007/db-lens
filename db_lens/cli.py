"""db-lens command line: connect, profiles, tables, inspect, query.

Thin presentation layer over db.py/sqlguard.py/store.py. Engine verification status (live vs. mocked) is stated in db.py and in
the README; in short: SQLite/PostgreSQL/MariaDB are tested on live servers, Oracle only against mocks.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, nullcontext
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, cast

import typer
from rich.console import Console, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from sqlalchemy import column, make_url, select, table, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlglot import exp

from . import __version__, conn, db, sqlguard, store, tunnel

out, err = Console(), Console(stderr=True)
app = typer.Typer(add_completion=True, help="db-lens: inspect and query any SQL database.", pretty_exceptions_show_locals=False)
profiles_app = typer.Typer(
    help="List, select and remove saved connection profiles.", invoke_without_command=True, pretty_exceptions_show_locals=False
)
app.add_typer(profiles_app, name="profiles")


def complete_profile(incomplete: str) -> list[str]:
    """Shell completion: saved profile names (never raises: a broken config just completes nothing)."""
    try:
        return [n for n in store.load()["profiles"] if n.startswith(incomplete)]
    except Exception:  # noqa: BLE001
        return []


ProfileName = Annotated[str, typer.Argument(autocompletion=complete_profile, help="Saved profile name.")]
Profile = Annotated[Optional[str], typer.Option("--profile", "-p", autocompletion=complete_profile, help="Profile (default: last connected).")]
Schema = Annotated[Optional[str], typer.Option("--schema", "-S", help="Schema/owner (default: profile or dialect default).")]
Timeout = Annotated[float, typer.Option("--timeout", "-t", min=0, help="Statement timeout in seconds (0 = none).")]
Pager = Annotated[bool, typer.Option(help="Page long table output.")]
_FORMULA = ("=", "+", "-", "@", "\t", "\r")  # spreadsheet formula triggers (OWASP CSV injection)
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # keeps \n and \t


class _Redact(logging.Filter):
    """Verbose output must never leak a secret, and library notices (which carry a `code`) are already shown by the CLI itself."""

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "code"):
            return False
        record.msg, record.args = store.redact(record.getMessage()), ()
        record.exc_info = record.exc_text = record.stack_info = None  # tracebacks and locals could carry what redact() cannot know about
        return True


def setup_logging(verbose: bool) -> None:
    log = logging.getLogger("db_lens")
    for h in [h for h in log.handlers if getattr(h, "_dbl_verbose", False)]:
        log.removeHandler(h)
    log.setLevel(logging.WARNING)
    if verbose:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("db-lens [debug] %(message)s"))
        h.addFilter(_Redact())
        h._dbl_verbose = True  # type: ignore[attr-defined]
        log.addHandler(h)
        log.setLevel(logging.DEBUG)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"db-lens {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version and exit.")] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", envvar="DB_LENS_VERBOSE", help="Diagnostics on stderr (no SQL text, no secrets).")
    ] = False,
) -> None:
    """db-lens: inspect and query any SQL database."""
    setup_logging(verbose)


class Fmt(str, Enum):
    table = "table"
    json = "json"
    csv = "csv"


def clean(v: object) -> str:
    """Make database content safe to print: control/escape bytes become visible \\xNN (no terminal injection)."""
    return _CTRL.sub(lambda m: f"\\x{ord(m[0]):02x}", str(v))


def csv_safe(v: object) -> object:
    """Neutralise spreadsheet formulas: text starting with = + - @ TAB CR gets a leading apostrophe. Numbers are untouched."""
    return "'" + v if isinstance(v, str) and v.startswith(_FORMULA) else v


class _LfCsvWriter:
    """CSV rows ending in a bare LF; a bare CR/LF inside a cell is still quoted."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def writerow(self, row: Iterable[object]) -> None:
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerow(row)
        self._stream.write(buf.getvalue()[:-2] + "\n")

    def writerows(self, rows: Iterable[Iterable[object]]) -> None:
        for r in rows:
            self.writerow(r)


def csv_writer(stream: Any) -> _LfCsvWriter:
    return _LfCsvWriter(stream)


def warn(msg: object) -> None:
    err.print(Text(f"! {msg}", style="yellow"))  # a db.Notice renders as "message [CODE]"


SECTIONS = (
    ("custom-routine", "Custom routines (user-created; can write regardless of grants)"),
    ("unresolved-role", "Roles that could not be fully resolved"),
    ("extension-routine", "Standard / extension functions (informational, not counted against --strict)"),
)


def risk_dicts(risks: list[Any]) -> list[dict[str, Any]]:
    return [r._asdict() if hasattr(r, "_asdict") else r for r in risks]


def blocking(stored: list[Any]) -> list[dict[str, Any]]:
    """Stored risks (legacy v0.5/0.6 profiles kept plain strings, all of which were blocking)."""
    return [
        r if isinstance(r, dict) else {"kind": "unknown", "message": r, "blocking": True, "names": []}
        for r in stored
        if isinstance(r, str) or r.get("blocking", True)
    ]


def risk_panel(writable: bool | None, why: str, risks: list[Any]) -> Panel | None:
    """Group risks by kind so user-created routines stand apart from standard/extension ones. None = nothing worth showing."""
    items = risk_dicts(risks)
    if not writable and not any(r["blocking"] for r in items):
        return None
    lines = [f"This account can modify data ({why})."] if writable else []
    for kind, title in SECTIONS:
        if group := [r for r in items if r["kind"] == kind]:
            lines += [f"{title}:"] + [f"  • {r['message']}" for r in group]
    return Panel(
        Text("\n".join(lines) + f"\n{sqlguard.NOTICE}"), title="Write-capable account" if writable else "Account risks", border_style="yellow"
    )


def esc(v: object) -> str:
    return escape(clean(v))


@contextmanager
def guard() -> Iterator[None]:
    """Turn every failure into a red panel with a hint; secrets are redacted; no stack traces."""
    try:
        yield
    except (typer.Exit, SystemExit, typer.Abort):
        raise
    except BrokenPipeError as e:
        raise typer.Exit(0) from e
    except KeyboardInterrupt as e:
        raise typer.Exit(130) from e
    except Exception as e:  # noqa: BLE001
        msg = store.redact(str(getattr(e, "orig", None) or e).strip())
        low = msg.lower()
        hint = (
            "Connection lost; retry."
            if getattr(e, "connection_invalidated", False)
            else "Timed out (raise --timeout)."
            if any(k in low for k in ("timeout", "timed out", "interrupt", "canceling statement", "max_execution_time", "dpy-4024"))
            else ""
        )
        code = db.error_code(e)
        err.print(
            Panel(
                Text(clean(msg)[:600] + (f"\n→ {hint}" if hint else "")),
                title=Text(type(e).__name__ + (f" [{code}]" if code else "")),
                border_style="red",
            )
        )
        raise typer.Exit(1) from e


@contextmanager
def session(profile: str | None) -> Iterator[tuple[Engine, dict[str, Any]]]:
    """Resolve a profile into a live engine that is always disposed."""
    name, rec, u = store.resolve(profile)
    logging.getLogger("db_lens").debug(
        "profile %r -> %s%s",
        name,
        u.render_as_string(hide_password=True),
        f" via ssh {tunnel.SshSpec.from_record(rec['ssh']).label()}" if rec.get("ssh") else "",
    )
    with tunnel.forward(u, tunnel.SshSpec.from_record(rec["ssh"])) if rec.get("ssh") else nullcontext(u) as net, db.engine(net) as eng:
        if rec.get("tls"):
            db.assert_tls(eng, rec["tls"])  # fail closed on every command, not just at connect time
        yield eng, rec


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def show(renderable: RenderableType, rows: int, pager: bool) -> None:
    if pager and out.is_terminal and rows + 6 > out.height:
        with out.pager(styles=True):
            out.print(renderable)
    else:
        out.print(renderable)


def render(title: str, cols: list[Any], rows: list[Any], max_cell: int = 80) -> Table:
    tbl = Table(title=Text(title), header_style="bold cyan")
    for c in cols:
        tbl.add_column(Text(clean(c)), overflow="fold")  # names fold, never truncate
    for r in rows:
        tbl.add_row(
            *(Text("NULL", style="dim italic") if v is None else Text(s if len(s := clean(v)) <= max_cell else s[: max_cell - 1] + "…") for v in r)
        )
    return tbl


def _uri_source(uri: str | None, uri_env: str | None, uri_file: Path | None) -> str | None:
    if sum(x is not None for x in (uri, uri_env, uri_file)) > 1:
        raise ValueError("Give only one of: the URI argument, --uri-env, --uri-file.")
    if uri_env is not None:
        if uri_env not in os.environ:
            raise ValueError(f"Environment variable {uri_env} is not set.")
        return os.environ[uri_env]
    if uri_file is not None:
        lines = [x.strip() for x in uri_file.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
        if not lines:
            raise ValueError(f"{uri_file} is empty.")
        return lines[0]
    return sys.stdin.readline().strip() if uri == "-" else uri


def _password(ask: bool, env: str | None, stdin: bool, uri_from_stdin: bool) -> str | None:
    if sum([ask, env is not None, stdin]) > 1:
        raise ValueError("Give only one of: --ask-password, --password-env, --password-stdin.")
    if ask:
        return str(typer.prompt("Password", hide_input=True))
    if env is not None:
        if not re.fullmatch(r"\w+", env):
            raise ValueError("--password-env expects a variable NAME (letters, digits, underscore), not the password.")
        return "${" + env + "}"  # stored as a reference; resolved from the environment on every run
    if stdin:
        if uri_from_stdin:
            raise ValueError("The URI and the password cannot both be read from stdin.")
        pw = sys.stdin.readline().rstrip("\r\n")
        if not pw:
            raise ValueError("--password-stdin received an empty line.")
        return pw
    return None


@app.command()
def connect(
    uri: Annotated[Optional[str], typer.Argument(help="URI, or Oracle SQL Developer style user/password@host:port/SID, or '-' for stdin.")] = None,
    profile_name: Annotated[Optional[str], typer.Option("--profile-name", "-n", help="Name for this profile.")] = None,
    schema: Schema = None,
    plaintext: Annotated[bool, typer.Option(help="Store the password in the config file, not the keyring.")] = False,
    uri_env: Annotated[Optional[str], typer.Option(help="Read the URI from this environment variable.")] = None,
    uri_file: Annotated[Optional[Path], typer.Option(help="Read the URI from the first line of this file.")] = None,
    dialect: Annotated[Optional[str], typer.Option("--dialect", "-D", help="sqlite | postgresql | mysql | mariadb | oracle (no URI needed).")] = None,
    driver: Annotated[Optional[str], typer.Option(help="Driver override, e.g. psycopg2, pymysql, mysqldb, mysqlconnector, oracledb.")] = None,
    host: Annotated[Optional[str], typer.Option(help="Server host.")] = None,
    port: Annotated[Optional[int], typer.Option(help="Server port.")] = None,
    user: Annotated[Optional[str], typer.Option("--user", "-u", help="User name.")] = None,
    database: Annotated[Optional[str], typer.Option("--database", "-d", help="Database name (sqlite: the file path).")] = None,
    ask_password: Annotated[bool, typer.Option("--ask-password", "-W", help="Prompt for the password (hidden; stays out of shell history).")] = False,
    password_env: Annotated[Optional[str], typer.Option(help="Take the password from this environment variable NAME at every run.")] = None,
    password_stdin: Annotated[bool, typer.Option(help="Read the password from the first line of stdin.")] = False,
    ssl_mode: Annotated[Optional[str], typer.Option(help="disable | require | verify-ca | verify-full (verified after connecting).")] = None,
    ssl_ca: Annotated[Optional[str], typer.Option(help="CA certificate file.")] = None,
    ssl_cert: Annotated[Optional[str], typer.Option(help="Client certificate file.")] = None,
    ssl_key: Annotated[Optional[str], typer.Option(help="Client private key file.")] = None,
    option: Annotated[Optional[list[str]], typer.Option(help="Extra driver option KEY=VALUE (repeatable), e.g. --option service_name=XE.")] = None,
    service: Annotated[bool, typer.Option("--service", help="Oracle: NAME in user/password@host:port/NAME is a SERVICE NAME, not a SID.")] = False,
    ssh: Annotated[Optional[str], typer.Option("--ssh", help="Reach the database through an SSH tunnel: [user@]host[:port].")] = None,
    ssh_key: Annotated[Optional[str], typer.Option(help="Private key file for --ssh.")] = None,
    ssh_accept_new: Annotated[bool, typer.Option(help="Trust an unknown SSH host key on first contact only.")] = False,
) -> None:
    """Test and save a profile (becomes default).

    Pass a URI, or build one from options so nothing needs shell quoting:
    db-lens connect -D postgresql --host db.example.com -d shop -u me --ask-password --ssl-mode verify-full --ssl-ca ca.pem
    """
    with guard():
        text_uri = _uri_source(uri, uri_env, uri_file)
        pw = _password(ask_password, password_env, password_stdin, uri_from_stdin=uri == "-")
        if pw and not store.has_ref(pw):
            store.remember(pw)  # from now on no error message can echo it, even one raised while the URI is still being parsed
        url = conn.build_url(
            uri=text_uri,
            dialect=dialect,
            driver=driver,
            host=host,
            port=port,
            user=user,
            database=database,
            password=pw,
            oracle_service=service,
            ssl_mode=ssl_mode,
            ssl_ca=ssl_ca,
            ssl_cert=ssl_cert,
            ssl_key=ssl_key,
            options=conn.parse_options(option),
        )
        tls = conn.required_tls(url, ssl_mode)
        uri_str = url.render_as_string(hide_password=False)
        live = url.set(password=store.expand(url.password)) if url.password and store.has_ref(url.password) else url
        store.remember(live.password)
        spec = tunnel.parse_target(ssh, ssh_key, ssh_accept_new) if ssh else None
        if spec is None and (ssh_key or ssh_accept_new):
            raise ValueError("--ssh-key and --ssh-accept-new need --ssh.")
        with tunnel.forward(live, spec) if spec else nullcontext(live) as net, db.engine(net) as eng, eng.connect():
            if tls:
                db.assert_tls(eng, tls)
            if schema:  # fail now, and store the canonical spelling (e.g. 'MAIN' -> 'main')
                schema = db.resolve_schema(sa_inspect(eng), schema)
            writable, why, risks = db.probe_account(eng)  # one probe, one connection
        name = profile_name or (
            Path(url.database).stem if url.get_backend_name() == "sqlite" and url.database else url.database or url.host or url.get_backend_name()
        )
        where = store.add(name, uri_str, schema, plaintext, writable, risk_dicts(risks), tls, spec.record() if spec else None)
        out.print(
            f"[green]✔[/] Connected [bold]{esc(name)}[/] [dim]{esc(url.render_as_string(hide_password=True))}[/] "
            f"· secret: {where}" + (f" · TLS: {tls} (verified)" if tls else "") + (f" · via ssh {spec.label()}" if spec else "")
        )
        if url.get_backend_name() != "sqlite" and (panel := risk_panel(writable, why, risks)):
            err.print(panel)
        if where == "plaintext":
            out.print("[yellow]![/] Password stored in plaintext (chmod 600). Install `keyring` or use ${ENV_VAR}.")
        if uri and uri != "-" and url.password and not store.has_ref(url.password):
            warn(
                "the password was given on the command line (visible in shell history and process lists); "
                "prefer --ask-password, --password-env or --password-stdin."
            )


@app.command()
def gui(profile: Profile = None) -> None:
    """Open the desktop window: run read-only queries and browse/export results (needs Tk; `pip install "db-lens[gui]"` adds the theme)."""
    with guard():
        if profile is not None and profile not in store.load()["profiles"]:
            raise ValueError(f"Profile {profile!r} not found. Run: db-lens connect <uri>")
        try:
            from tkinter import TclError

            from .gui import app as gui_app
        except ImportError as e:
            raise ValueError("The desktop window needs Tk (Linux: install python3-tk; the python.org installers include it).") from e
        try:
            gui_app.main(profile)
        except TclError as e:  # no display (ssh session, container) or a broken Tk install
            raise ValueError(f"Cannot open the window: {e}") from e


def _profile_records() -> list[dict[str, Any]]:
    cfg = store.load()
    recs = []
    for n, r in cfg["profiles"].items():
        recs.append(
            {
                "name": n,
                "default": n == cfg["default"],
                "uri": make_url(r["uri"]).render_as_string(hide_password=True),
                "schema": r.get("schema"),
                "secret": store.secret_kind(r),
                "tls": r.get("tls"),
                "ssh": tunnel.SshSpec.from_record(r["ssh"]).label() if r.get("ssh") else None,
                "account": {True: "write-capable", False: "read-only"}.get(r.get("writable"), "unknown"),
                "risks": len(blocking(r.get("risks", []))),
            }
        )
    return recs


def _profile_rows() -> Table:
    tbl = Table(header_style="bold cyan")
    for h in ("Profile", "URI", "Schema", "Secret", "TLS", "SSH", "Account"):
        tbl.add_column(h, overflow="fold")  # narrow terminals wrap a cell instead of cutting it with an ellipsis
    for r in _profile_records():
        tbl.add_row(
            Text(clean(r["name"]) + (" ★" if r["default"] else "")),
            Text(clean(r["uri"])),
            Text(r["schema"] or "—"),
            r["secret"],
            Text(r["tls"] or "—"),
            Text(clean(r["ssh"] or "—")),
            r["account"].upper() if r["account"] == "write-capable" else r["account"] + (f" (+{r['risks']} risk)" if r["risks"] else ""),
        )
    return tbl


def emit_records(records: list[dict[str, Any]], fmt: Fmt, meta: str) -> None:
    """Machine-readable output on stdout (JSON, or CSV with spreadsheet formulas neutralised); the summary goes to stderr."""
    if fmt is Fmt.json:
        typer.echo(json.dumps(records, default=str, indent=2))
    else:
        w = csv_writer(sys.stdout)
        cols = list(records[0]) if records else []
        w.writerow(cols)
        w.writerows([csv_safe(r[c]) for c in cols] for r in records)
    err.print(Text(meta, style="dim"))


Format = Annotated[Fmt, typer.Option("--format", "-o", help="table | json | csv")]


def _list_profiles(fmt: Fmt) -> None:
    if fmt is Fmt.table:
        out.print(_profile_rows())
    else:
        recs = _profile_records()
        emit_records(recs, fmt, f"{len(recs)} profile(s)")


@profiles_app.callback()
def profiles(ctx: typer.Context, fmt: Format = Fmt.table) -> None:
    """List saved profiles (no sub-command), or manage them: `profiles use|remove|rename NAME`."""
    if ctx.invoked_subcommand is None:
        with guard():
            _list_profiles(fmt)


@profiles_app.command("list")
def profiles_list(fmt: Format = Fmt.table) -> None:
    """List saved profiles."""
    with guard():
        _list_profiles(fmt)


@profiles_app.command("rename")
def profiles_rename(old: ProfileName, new: Annotated[str, typer.Argument(help="New name.")]) -> None:
    """Rename a profile (keeps its keyring password and default status)."""
    with guard():
        moved = store.rename(old, new)
        out.print(f"[green]✔[/] Renamed [bold]{esc(old)}[/] to [bold]{esc(new)}[/]" + (" (keyring password moved)" if moved else "") + ".")


@profiles_app.command("use")
def profiles_use(name: ProfileName) -> None:
    """Make NAME the default profile."""
    with guard():
        store.use(name)
        out.print(f"[green]✔[/] Default profile is now [bold]{esc(name)}[/].")


@profiles_app.command("remove")
def profiles_remove(name: ProfileName, yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False) -> None:
    """Delete profile NAME and its keyring password (the database itself is never touched)."""
    with guard():
        cfg = store.load()
        if name not in cfg["profiles"]:
            raise store._missing(name, cfg)
        if not yes:
            typer.confirm(f"Remove profile {name!r}?", abort=True)
        secret, default = store.remove(name)
        out.print(f"[green]✔[/] Removed [bold]{esc(name)}[/]" + (" and its keyring password" if secret else "") + ".")
        if cfg["default"] == name:
            out.print(
                f"Default profile is now [bold]{esc(default)}[/]."
                if default
                else "[yellow]![/] No default profile; choose one with `db-lens profiles use NAME`."
            )


@app.command()
def tables(
    profile: Profile = None,
    schema: Schema = None,
    pager: Pager = True,
    fmt: Format = Fmt.table,
    search: Annotated[Optional[str], typer.Option("--search", "-s")] = None,
) -> None:
    """List tables with column counts and row/size metrics (~ = catalog estimate; ? = unavailable)."""
    with guard(), session(profile) as (eng, rec):
        insp = sa_inspect(eng)
        sch = db.resolve_schema(insp, schema or rec.get("schema"))
        names = sorted(t for t in insp.get_table_names(schema=sch) if not search or search.lower() in t.lower())
        if not names and fmt is Fmt.table:
            return out.print("[yellow]No tables found.[/]")
        cols, est = db.column_counts(insp, sch, names), db.estimates(eng, sch, names)
        approx = "~" if eng.dialect.name in db.EST else ""
        if fmt is not Fmt.table:
            recs = [
                {
                    "table": t,
                    "columns": cols.get(t),
                    "rows": (lambda r: None if r is None else int(r))(est.get(t, (None, None))[0]),
                    "rows_estimated": bool(approx),
                    "size_bytes": (lambda b: None if b is None else int(b))(est.get(t, (None, None))[1]),
                }
                for t in names
            ]
            return emit_records(recs, fmt, f"{len(recs)} table(s) · {eng.dialect.name} · {sch}")
        tbl = Table(title=Text(f"{eng.dialect.name} · {sch} · {len(names)} table(s)"), header_style="bold cyan")
        for h, j in (("Table", "left"), ("Columns", "right"), ("Rows", "right"), ("Size", "right")):
            tbl.add_column(h, justify=cast(Literal["left", "right"], j), style="bold" if h == "Table" else "green" if h == "Rows" else "")
        for t in names:
            r, s = est.get(t, (None, None))
            tbl.add_row(Text(clean(t)), str(cols.get(t, "?")), "?" if r is None else f"{approx}{int(r):,}", "—" if s is None else human(int(s)))
        show(tbl, len(names), pager)
        if eng.dialect.name == "sqlite":
            out.print(f"[dim]{db.sqlite_summary(eng, sch)}[/]")


def safe(f: Callable[..., Any], *a: Any, **k: Any) -> Any:
    try:
        return f(*a, **k)
    except (NotImplementedError, SQLAlchemyError):
        return None


def branch(tree: Tree, title: str, items: list[str]) -> None:
    if items:
        b = tree.add(f"[bold]{title}[/]")
        for i in items:
            b.add(i)


@app.command()
def inspect(
    table_name: str,
    profile: Profile = None,
    schema: Schema = None,
    timeout: Timeout = 30,
    max_cell: Annotated[int, typer.Option(help="Truncate sample cells to N chars.")] = 80,
    sample: Annotated[int, typer.Option("--sample", "-n", min=0, help="Sample rows to show.")] = 0,
) -> None:
    """Show columns, keys, indexes, constraints, partitioning and optional sample rows."""
    with guard(), session(profile) as (eng, rec):
        insp = sa_inspect(eng)
        sch = db.resolve_schema(insp, schema or rec.get("schema"))
        t = db.resolve_table(insp, table_name, sch)
        cols = insp.get_columns(t, schema=sch)
        pks = set((safe(insp.get_pk_constraint, t, schema=sch) or {}).get("constrained_columns") or [])  # restricted accounts: skip
        fks = {
            c: f"{fk['referred_table']}.{r}"
            for fk in safe(insp.get_foreign_keys, t, schema=sch) or []
            for c, r in zip(fk["constrained_columns"], fk["referred_columns"])
        }
        tree = Tree(f"[bold]{esc(t)}[/] [dim]{esc(sch)}[/]")
        branch(
            tree,
            "Columns",
            [
                f"[cyan]{esc(c['name'])}[/] [dim]{esc(c['type'])}[/]"
                + ("" if c["nullable"] else " [red]NOT NULL[/]")
                + (" [yellow]🔑 PK[/]" if c["name"] in pks else "")
                + (f" [magenta]→ {esc(fks[c['name']])}[/]" if c["name"] in fks else "")
                + (f" [dim]default {esc(c['default'])}[/]" if c.get("default") is not None else "")
                for c in cols
            ],
        )
        branch(
            tree,
            "Indexes",
            [
                f"{esc(i.get('name'))} ({esc(', '.join(x or 'expr' for x in i['column_names']))})" + (" [yellow]UNIQUE[/]" if i.get("unique") else "")
                for i in safe(insp.get_indexes, t, schema=sch) or []
            ],
        )
        uniques = [
            f"UNIQUE {esc(u['name']) + ' ' if u.get('name') else ''}({esc(', '.join(u['column_names']))})"
            for u in safe(insp.get_unique_constraints, t, schema=sch) or []
        ]
        checks = [
            f"CHECK {esc(k['name']) + ' ' if k.get('name') else ''}{esc(k['sqltext'])}" for k in safe(insp.get_check_constraints, t, schema=sch) or []
        ]
        branch(tree, "Constraints", uniques + checks)
        branch(
            tree,
            "Partitioning",
            [f"{esc(k)}: {esc(v)}" for k, v in (safe(insp.get_table_options, t, schema=sch) or {}).items() if "partition" in k and v],
        )
        out.print(Panel(tree, title="Schema", border_style="blue"))
        if sample:  # identifiers are quoted by SQLAlchemy's dialect compiler, never interpolated
            q = select(table(t, *(column(c["name"]) for c in cols), schema=sch)).limit(sample)
            with db.connect_ro(eng, sch, timeout) as c:
                cols_, rows, _ = db.fetch_limited(c, q, sample)
                for w in c.info.get("warnings", []):
                    warn(w)
            out.print(render(f"Sample ({len(rows)} rows)", cols_, rows, max_cell))


@app.command()
def query(
    sql: Annotated[Optional[str], typer.Argument(help="SQL; '-' or omitted reads stdin.")] = None,
    file: Annotated[Optional[Path], typer.Option("--file", "-f", exists=True, dir_okay=False)] = None,
    profile: Profile = None,
    schema: Schema = None,
    timeout: Timeout = 30,
    limit: Annotated[int, typer.Option("--limit", "-l", min=1)] = 100,
    fmt: Annotated[Fmt, typer.Option("--format", "-o")] = Fmt.table,
    max_cell: Annotated[int, typer.Option(help="Truncate table cells to N chars.")] = 80,
    pager: Pager = True,
    strict: Annotated[bool, typer.Option(help="Refuse to run unless the account is verifiably read-only.")] = False,
    csv_raw: Annotated[bool, typer.Option(help="CSV: do not neutralise spreadsheet formulas (=,+,-,@).")] = False,
) -> None:
    """Run one statement. The SQL check is best-effort (UDFs/procedures can write): use a dedicated read-only
    database role. A read-only session is enabled and verified on top (--strict also requires a read-only account)."""
    with guard():
        if file:
            stmt = file.read_text()
        elif sql not in (None, "-"):
            stmt = sql
        elif sys.stdin.isatty():
            raise ValueError("Provide SQL, --file, or pipe SQL on stdin.")
        else:
            stmt = sys.stdin.read()
        stmt = stmt.strip().rstrip(";")  # Oracle rejects a trailing ';'
        with session(profile) as (eng, rec):
            root = sqlguard.validate(stmt, eng.dialect.name)
            logging.getLogger("db_lens").debug("statement accepted as read-only (%s)", type(root).__name__)
            sch = schema or rec.get("schema")
            if sch:
                sch = db.resolve_schema(sa_inspect(eng), sch)
            stored = blocking(rec.get("risks", []))
            routines = {n for r in stored if r["kind"] == "custom-routine" for n in r.get("names", [])}
            hits, udfs = sqlguard.classify_calls(root, routines)
            if strict:
                ok, why, risks = db.probe_account(eng)
                bl = [r.message for r in risks if r.blocking]
                if ok is not False or bl or hits or udfs:
                    detail = why or "; ".join(bl) or f"statement calls {', '.join(hits + udfs)}"
                    raise ValueError(f"--strict: not verifiably read-only ({detail or 'unknown'}). {sqlguard.NOTICE}")
            elif eng.dialect.name != "sqlite" and (rec.get("writable") or stored):
                warn(
                    "account can modify data or execute user-created routines; SQL checks cannot see inside UDFs/procedures. "
                    "Use a read-only role without EXECUTE (--strict)."
                )
            if hits:
                warn(f"calls user-created routine(s) {', '.join(hits[:5])}: the SQL check cannot see what they do; they may write.")
            if udfs:
                warn(f"calls {', '.join(udfs[:5])}: if user-defined, the SQL check cannot see what it does (may write).")
            stream = isinstance(root, exp.Query) and eng.dialect.name in ("postgresql", "mysql", "mariadb")
            t0 = time.perf_counter()
            with db.connect_ro(eng, sch, timeout) as c:
                for w in c.info.get("warnings", []):
                    warn(w)
                cols, rows, trunc = db.fetch_limited(c, text(stmt), limit, stream)
        ms = (time.perf_counter() - t0) * 1000
        logging.getLogger("db_lens").debug("fetched %d row(s) in %.1f ms", len(rows), ms)
        meta = f"{len(rows)} row(s){' (truncated; raise --limit)' if trunc else ''} · {ms:.1f} ms"
        if fmt is Fmt.json:
            typer.echo(json.dumps([dict(zip(cols, r)) for r in rows], default=str, indent=2))
        elif fmt is Fmt.csv:
            w, f = csv_writer(sys.stdout), (lambda v: v) if csv_raw else csv_safe  # \r\n would become \r\r\n on Windows
            w.writerow([f(c) for c in cols])  # aliases are attacker-controllable too
            w.writerows([f(v) for v in r] for r in rows)
        else:
            show(render(meta, cols, rows, max_cell), len(rows), pager)
            return
        err.print(Text(meta, style="dim"))


def run() -> None:
    """Console entry point: nothing escapes as a traceback (messages are redacted, locals are never printed)."""
    try:
        app(prog_name="db-lens")
    except Exception as e:  # noqa: BLE001 - last line of defence for failures outside guard()
        err.print(Panel(Text(clean(store.redact(str(e)))[:600]), title=Text(type(e).__name__), border_style="red"))
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
