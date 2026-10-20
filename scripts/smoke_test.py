#!/usr/bin/env python3
"""Unattended run of TESTING.md: drives the installed `db-lens` CLI exactly like the manual plan and prints PASS / FAIL per step.

    python scripts/smoke_test.py                    # Parts 0-4 on a throw-away SQLite database (about a minute)
    python scripts/smoke_test.py --with-packaging   # + Part 5: ruff, mypy, pytest, build, twine, release gate (needs `pip install -e ".[dev]"`)
    python scripts/smoke_test.py --with-keyring     # + step 4.6 against the real OS credential store
    python scripts/smoke_test.py --live             # + Part 7 using DBLENS_PG_URL / DBLENS_MYSQL_URL (throw-away servers only)
    python scripts/smoke_test.py --report out.md    # also write a Markdown result table you can paste into an issue

Works on Windows, Linux and macOS. Config, HOME and the test database live in a temporary directory, so your own profiles are never
touched. Exit code 0 = every executed step passed (skipped and manual-only steps are listed). No third-party imports except in --live."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = """
CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, note TEXT);
CREATE TABLE parent(a INT, b INT, PRIMARY KEY(a,b));
CREATE TABLE "order items"(id INTEGER PRIMARY KEY, "unit price" REAL NOT NULL CHECK("unit price">=0), pa INT, pb INT, sku TEXT UNIQUE, FOREIGN KEY(pa,pb) REFERENCES parent(a,b));
CREATE INDEX ix_sku2 ON "order items"(sku,"unit price");
CREATE VIEW v_users AS SELECT id,name FROM users;
INSERT INTO parent VALUES (1,2);
INSERT INTO "order items" VALUES (1,9.5,1,2,'s1');
INSERT INTO users VALUES (1,'Ann','=1+1'),(2,'Bob','-5'),(3,NULL,'@SUM(A1)'),(4,'[red]bold[/red]','+1 555'),(5,char(27)||'[31mEVIL',char(9)||'tab'),(6,'NULL',''),(7,'x','plain');
"""
MANUAL = [
    "3.6 open the CSV in Excel / LibreOffice and confirm no cell evaluates",
    "6.x GitHub rendering and the Actions tab",
    "0.x glyphs (checkmark, star, key) display correctly in your terminal font",
]


class Proc(NamedTuple):
    out: str
    err: str
    code: int
    raw: bytes

    @property
    def both(self) -> str:
        return self.out + self.err


def flat(s: str) -> str:
    """Collapse table borders and whitespace so assertions survive Rich's wrapping."""
    return " ".join(re.sub("[│┃┆|─━┌┐└┘├┤┬┴┼╭╮╰╯]", " ", s).split())


class Smoke:
    def __init__(self, tmp: Path) -> None:
        self.tmp, self.home = tmp, tmp / "home"
        self.home.mkdir()
        self.cfg = self.home / ".db-lens" / "config.json"
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "DB_LENS_CONFIG": str(self.cfg),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "COLUMNS": "220",
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
        self.results: list[tuple[str, str, str, str]] = []
        self.db = self.home / "dbl_test" / "shop.db"
        self.uri = ""

    # -- plumbing ------------------------------------------------------------------------------------------------
    def run(self, argv: list[str], input: str | None = None, timeout: int = 120, env: dict[str, str] | None = None, cwd: Path | None = None) -> Proc:
        p = subprocess.run(
            argv,
            input=input.encode() if input is not None else None,
            capture_output=True,
            timeout=timeout,
            env=env or self.env,
            cwd=cwd,
            stdin=None if input is not None else subprocess.DEVNULL,
        )
        return Proc(p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"), p.returncode, p.stdout)

    def cli(self, *args: str, input: str | None = None, env: dict[str, str] | None = None) -> Proc:
        return self.run([sys.executable, "-m", "db_lens", *args], input=input, env=env)

    def check(self, sid: str, title: str, ok: bool, detail: str = "", skip: str = "") -> None:
        status = "SKIP" if skip else "PASS" if ok else "FAIL"
        self.results.append((sid, title, status, skip or ("" if ok else detail)))
        print(f"  [{status}] {sid:5} {title}" + ("" if ok or skip else f"\n         -> {self.ascii(detail)[:600]}"), flush=True)

    @staticmethod
    def ascii(s: str) -> str:
        return s.encode("ascii", "replace").decode()

    def expect(
        self, sid: str, title: str, p: Proc, *, code: int = 0, has: tuple[str, ...] = (), hasnt: tuple[str, ...] = (), where: str = "both"
    ) -> Proc:
        text = flat(getattr(p, where))
        problems = (
            ([f"exit code {p.code}, expected {code}"] if p.code != code else [])
            + [f"missing {h!r}" for h in has if flat(h) not in text]
            + [f"unexpected {h!r}" for h in hasnt if flat(h) in text]
        )
        if "Traceback (most recent call last)" in p.both:
            problems.append("Python traceback in output")
        self.check(sid, title, not problems, "; ".join(problems) + "\n" + p.both[-500:])
        return p

    # -- parts ---------------------------------------------------------------------------------------------------
    def part0(self) -> None:
        print("Part 0: installation")
        h = self.cli("--help")
        self.expect("0.6", "db-lens --help lists the commands", h, has=("connect", "profiles", "tables", "inspect", "query"))
        self.expect("0.7", "python -m db_lens --version", self.cli("--version"), has=("db-lens 0",))
        exe = shutil.which("db-lens")
        if exe:
            self.expect("0.8", "console script `db-lens` works", self.run([exe, "--help"]), has=("connect", "query"))
        else:
            self.check("0.8", "console script `db-lens` works", True, skip="db-lens is not on PATH (activate the venv)")

    def part1(self) -> None:
        print("Part 1: test database")
        self.db.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.db)
        c.executescript(SCHEMA)
        c.commit()
        c.close()
        self.uri = "sqlite:///" + self.db.as_posix()
        self.check("1.1", "test database created", self.db.exists())

    def part2(self) -> None:
        print("Part 2: connect, browse, inspect")
        e, c = self.expect, self.cli
        e("2.1", "connect saves the profile", c("connect", self.uri, "-n", "shop"), has=("Connected shop", "secret: none"), hasnt=("Write-capable",))
        e("2.2", "profiles shows shop as default and write-capable", c("profiles"), has=("shop ★", "WRITE-CAPABLE"))
        t = e("2.3", "tables: 3 tables with row counts", c("tables"), has=("sqlite · main · 3 table(s)", "pages ×"), hasnt=("v_users",))
        n = flat(t.out)
        self.check("2.3b", "tables: column and row counts are exact", all(x in n for x in ("order items 5 1", "parent 2 1", "users 3 7")), n[:300])
        e("2.4", "tables --search is case-insensitive", c("tables", "-s", "USER"), has=("users",), hasnt=("parent",))
        e("2.5", "tables --search with no match", c("tables", "-s", "zzz"), has=("No tables found",))
        e("2.6", "unknown schema is a clean error", c("tables", "-S", "nope"), code=1, has=("Schema 'nope' not found. Available: main",))
        i = e(
            "2.7",
            "inspect shows keys, index, constraints, sample",
            c("inspect", "order items", "-n", "5"),
            has=("PK", "NOT NULL", "parent.a", "parent.b", "ix_sku2 (sku, unit price)", "UNIQUE (sku)", "CHECK", "Sample (1 rows)"),
            hasnt=("None",),
        )
        self.check("2.7b", "inspect: only the id column is a PK", flat(i.out).count("PK") == 1)
        p = c("inspect", "parent")
        self.check("2.8", "inspect: composite primary key", p.code == 0 and flat(p.out).count("PK") == 2, p.both[-300:])
        e("2.9", "inspect a view", c("inspect", "v_users"), has=("id", "name"))
        e("2.10", "inspect unknown table", c("inspect", "nope"), code=1, has=("not found",))
        e("2.11", "inspect with upper-case schema", c("inspect", "users", "-n", "3", "-S", "MAIN"), has=("Sample (3 rows)",))

    def part3(self) -> None:
        print("Part 3: queries, formats, data safety")
        e, c = self.expect, self.cli
        q = e(
            "3.1",
            "table output: NULLs, markup and escape bytes are neutralised",
            c("query", "SELECT id,name FROM users ORDER BY id"),
            has=("7 row(s)", "[red]bold[/red]", "\\x1b[31mEVIL", "NULL"),
        )
        self.check("3.1b", "no raw ESC byte reaches the terminal", "\x1b" not in q.both)
        e("3.2", "limit truncates and says so", c("query", "SELECT * FROM users", "-l", "3"), has=("3 row(s) (truncated; raise --limit)",))
        j = c("query", "SELECT id,name FROM users", "-o", "json")
        try:
            data = json.loads(j.out)
            ok = len(data) == 7 and data[2]["name"] is None
        except ValueError:
            ok = False
        self.check("3.3", "json output parses; NULL is null", ok, j.both[-300:])
        csv_out = c("query", "SELECT note FROM users", "-o", "csv")
        rows = [r[0] for r in csv.reader(io.StringIO(csv_out.out))][1:]
        want = ["'=1+1", "'-5", "'@SUM(A1)", "'+1 555", "'\ttab", "", "plain"]
        self.check("3.4", "csv neutralises formulas", rows == want and b"\r\r" not in csv_out.raw, f"{rows!r}")
        raw_rows = [r[0] for r in csv.reader(io.StringIO(c("query", "SELECT note FROM users", "-o", "csv", "--csv-raw").out))][1:]
        self.check("3.5", "--csv-raw leaves values untouched", raw_rows[:4] == ["=1+1", "-5", "@SUM(A1)", "+1 555"], f"{raw_rows!r}")
        e("3.7", "SQL from stdin", c("query", "-", input="SELECT count(*) AS n FROM users"), has=("n", "7"))
        sql_file = self.home / "q.sql"
        sql_file.write_text("SELECT 1 AS one;\n", encoding="utf-8")
        e("3.8", "SQL from a file", c("query", "-f", str(sql_file)), has=("one", "1"))
        e("3.9", "read-only PRAGMA", c("query", "PRAGMA table_info(users)"), has=("id", "name", "note"))
        w = c("query", "SELECT 1 AS c, '" + "a" * 300 + "' AS wide", "--max-cell", "20")
        self.check("3.10", "wide cell cut at --max-cell, header intact", "…" in w.out and "a" * 21 not in w.out and "wide" in w.out, w.both[-300:])
        t0 = time.time()
        r = c("query", "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c", "-t", "1")
        self.expect("3.11", "statement timeout aborts a runaway query", r, code=1, has=("interrupted", "Timed out"))
        self.check("3.11b", "...within a few seconds", time.time() - t0 < 15, f"{time.time() - t0:.1f}s")
        e("3.12", "unknown function: warning, then error", c("query", "SELECT my_fn(1)"), code=1, has=("calls my_fn", "no such function"))
        e(
            "3.13",
            "200 rows without the pager",
            c("query", "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 200) SELECT x FROM c", "-l", "200", "--no-pager"),
            has=("200 row(s)",),
        )
        bad = [
            "DELETE FROM users",
            "DROP TABLE users",
            "INSERT INTO users VALUES (9,'z','z')",
            "UPDATE users SET name='z'",
            "SELECT 1; DROP TABLE users",
            "WITH a AS (SELECT 1) DELETE FROM users",
            "PRAGMA user_version=5",
            "SELECT load_extension('x')",
        ]
        fails = [s for s in bad if not (lambda p: p.code == 1 and "UnsafeSQL" in p.both)(c("query", s))]
        self.check("3.14", f"{len(bad)} write/DDL statements are refused (UnsafeSQL, exit 1)", not fails, f"not refused: {fails}")
        ep = c("query", "-", input="")
        self.check("3.14b", "empty statement is refused", ep.code == 1 and ("Empty" in ep.both or "Provide" in ep.both), ep.both[-200:])
        e("3.15", "data unchanged after all of the above", c("query", "SELECT count(*) AS n FROM users"), has=("n", "7"))

    def part4(self, keyring: bool) -> None:
        print("Part 4: strict mode, config, secrets, profiles")
        e, c = self.expect, self.cli
        e("4.1", "--strict refuses a writable database file", c("query", "SELECT 1 AS x", "--strict"), code=1, has=("not verifiably read-only",))
        mode = self.db.stat().st_mode
        os.chmod(self.db, stat.S_IREAD)
        try:
            if os.access(self.db, os.W_OK):
                self.check("4.2", "--strict passes on a read-only file", True, skip="cannot make the file read-only for this user (running as root?)")
            else:
                e("4.2", "--strict passes on a read-only file", c("query", "SELECT 1 AS x", "--strict"), has=("x", "1"))
        finally:
            os.chmod(self.db, mode)
        cfg = self.cfg.read_text(encoding="utf-8")
        data = json.loads(cfg)
        self.check(
            "4.3", "config holds the profile, no password", "shop" in data["profiles"] and data["profiles"]["shop"].get("writable") is True, cfg[:300]
        )
        f = e(
            "4.4",
            "failed connect leaks no password and saves nothing",
            c("connect", "postgresql://bob:Sup3rSecretPw@127.0.0.1:1/x", "-n", "fake"),
            code=1,
            hasnt=("Sup3rSecretPw",),
        )
        self.check("4.4b", "...profile 'fake' was not stored", "fake" not in c("profiles").out, f.both[-200:])
        env = {**self.env, "DBL_PW": "envSecret99"}
        e(
            "4.5",
            "${ENV} password never printed",
            c("connect", "postgresql://bob:${DBL_PW}@127.0.0.1:1/x", "-n", "fake2", env=env),
            code=1,
            hasnt=("envSecret99",),
        )
        if keyring:
            code = (
                "import keyring; keyring.set_password('dbl-smoke','u','v'); print(keyring.get_password('dbl-smoke','u')); "
                "keyring.delete_password('dbl-smoke','u')"
            )
            k = self.run([sys.executable, "-c", code])
            self.check("4.6", "OS credential store round-trips a secret", k.out.strip() == "v", k.both[-300:])
        else:
            self.check("4.6", "OS credential store round-trips a secret", True, skip="use --with-keyring")
        saved = self.cfg.read_text(encoding="utf-8")
        self.cfg.write_text("{broken", encoding="utf-8")
        e("4.7", "corrupt config is reported, never overwritten", c("profiles"), code=1, has=("corrupt", "Not modified"))
        self.check("4.7b", "...file left intact", self.cfg.read_text(encoding="utf-8") == "{broken")
        self.cfg.write_text(saved, encoding="utf-8")
        self.check("4.9", "no temp files left in the config directory", not [p.name for p in self.cfg.parent.iterdir() if p.suffix == ".tmp"])
        # profiles use / remove
        e("4.10", "second and third profile", c("connect", "-D", "sqlite", "-d", str(self.db), "-n", "two"), has=("Connected two",))
        c("connect", "-D", "sqlite", "-d", str(self.db), "-n", "three")
        e("4.11", "profiles use", c("profiles", "use", "shop"), has=("Default profile is now shop",))
        e("4.12", "profiles remove asks first; 'n' keeps it", c("profiles", "remove", "two", input="n\n"), code=1, has=("Aborted",))
        self.check("4.12b", "...profile still there", "two" in c("profiles").out)
        e("4.13", "profiles remove --yes", c("profiles", "remove", "two", "--yes"), has=("Removed two",))
        e("4.14", "removing an unknown profile fails cleanly", c("profiles", "remove", "zzz", "-y"), code=1, has=("not found",))
        c("profiles", "remove", "three", "-y")
        e("4.15", "last profile left becomes the default", c("profiles", "remove", "shop", "-y"), has=("Removed shop",))
        e("4.16", "no profiles: clear hint", c("tables"), code=1, has=("Profile None not found",))
        # connect variants
        e(
            "4.17",
            "connect from structured options (no URI, no quoting)",
            c("connect", "-D", "sqlite", "-d", str(self.db), "-n", "s"),
            has=("Connected s",),
        )
        e("4.18", "connect --uri-env", c("connect", "--uri-env", "DBL_URI", "-n", "ue", env={**self.env, "DBL_URI": self.uri}), has=("Connected ue",))
        uf = self.home / "uri.txt"
        uf.write_text("\n" + self.uri + "\n", encoding="utf-8")
        e("4.19", "connect --uri-file", c("connect", "--uri-file", str(uf), "-n", "uf"), has=("Connected uf",))
        e("4.20", "connect with the URI on stdin", c("connect", "-", "-n", "ui", input=self.uri + "\n"), has=("Connected ui",))
        e(
            "4.21",
            "password via stdin never printed",
            c(
                "connect",
                "-D",
                "postgresql",
                "--host",
                "127.0.0.1",
                "--port",
                "1",
                "-u",
                "bob",
                "-d",
                "x",
                "--password-stdin",
                input="Sup3r$ecret!\n",
            ),
            code=1,
            hasnt=("Sup3r$ecret!",),
        )
        e(
            "4.22",
            "--ssl-mode on sqlite is rejected",
            c("connect", "-D", "sqlite", "-d", str(self.db), "--ssl-mode", "require"),
            code=1,
            has=("not supported for sqlite",),
        )
        e(
            "4.23",
            "secrets in --option are rejected",
            c("connect", "-D", "postgresql", "--host", "h", "--option", "password=x"),
            code=1,
            has=("looks like a secret",),
        )
        left = json.loads(self.cfg.read_text(encoding="utf-8"))["profiles"]
        self.check("4.24", "none of the failed connects saved anything", not any(n in left for n in ("fake", "fake2")), str(list(left)))
        # rename, machine-readable output, verbose, completion, ssh
        e("4.26", "profiles rename", c("profiles", "rename", "ui", "renamed"), has=("Renamed ui to renamed",))
        e("4.26b", "rename onto an existing name is refused", c("profiles", "rename", "renamed", "s"), code=1, has=("already exists",))
        e("4.26c", "renamed profile still works", c("tables", "-p", "renamed"), has=("3 table(s)",))
        pj = c("profiles", "-o", "json")
        try:
            plist = json.loads(pj.out)
            ok = {r["name"] for r in plist} >= {"s", "renamed"} and sum(r["default"] for r in plist) == 1
        except (ValueError, KeyError, TypeError):
            ok = False
        self.check("4.27", "profiles -o json parses (one default, no secrets)", ok and "password" not in pj.out.lower(), pj.both[-300:])
        pc = list(csv.reader(io.StringIO(c("profiles", "list", "-o", "csv").out)))
        self.check("4.27b", "profiles -o csv parses", pc[0][:3] == ["name", "default", "uri"] and len(pc) >= 3, str(pc[:2]))
        tj = c("tables", "-p", "renamed", "-o", "json")
        try:
            trec = {r["table"]: r for r in json.loads(tj.out)}
            ok = trec["users"]["rows"] == 7 and trec["users"]["columns"] == 3 and trec["order items"]["columns"] == 5
        except (ValueError, KeyError, TypeError):
            ok = False
        self.check("4.28", "tables -o json has exact counts", ok, tj.both[-300:])
        tc = list(csv.reader(io.StringIO(c("tables", "-p", "renamed", "-o", "csv", "-s", "user").out)))
        self.check(
            "4.28b",
            "tables -o csv parses",
            tc[:2] == [["table", "columns", "rows", "rows_estimated", "size_bytes"], tc[1]] and tc[1][:3] == ["users", "3", "7"],
            str(tc),
        )
        quiet, loud = c("query", "SELECT 'secret-literal' AS s", "-p", "renamed"), c("-v", "query", "SELECT 'secret-literal' AS s", "-p", "renamed")
        self.check(
            "4.29",
            "--verbose prints diagnostics, never the SQL text; silent otherwise",
            "[debug]" not in quiet.err
            and "db-lens [debug] profile 'renamed'" in loud.err
            and "secret-literal" not in loud.err
            and "read-only" in loud.err,
            loud.err[-400:],
        )
        comp_env = {**self.env, "_DB_LENS_COMPLETE": "complete_bash", "COMP_WORDS": "db-lens profiles use ren", "COMP_CWORD": "3"}
        comp = self.run([sys.executable, "-m", "db_lens"], env=comp_env)
        self.check("4.30", "shell completion offers saved profile names", "renamed" in comp.out and "ue" not in comp.out.split(), comp.both[-200:])
        e(
            "4.31",
            "--ssh refuses a host that ssh would read as an option",
            c("connect", "-D", "postgresql", "--host", "h", "--ssh", "-oProxyCommand=x"),
            code=1,
            has=("--ssh expects",),
        )
        e("4.31b", "--ssh does not apply to SQLite", c("connect", "-D", "sqlite", "-d", str(self.db), "--ssh", "bastion"), code=1, has=("SQLite",))
        if shutil.which("ssh"):
            e(
                "4.31c",
                "a dead SSH server is a clean panel",
                c(
                    "connect",
                    "-D",
                    "postgresql",
                    "--host",
                    "h",
                    "--port",
                    "5432",
                    "-u",
                    "u",
                    "-d",
                    "d",
                    "--ssh",
                    "nobody@127.0.0.1:1",
                    "-n",
                    "sshfail",
                ),
                code=1,
                has=("TunnelError",),
            )
            self.check("4.31d", "...and nothing was saved", "sshfail" not in c("profiles").out)
        else:
            self.check("4.31c", "a dead SSH server is a clean panel", True, skip="OpenSSH client `ssh` not on PATH")

    def part5(self) -> None:
        print("Part 5: lint, types, tests, packaging (repository checkout required)")
        steps = [
            ("5.1", "ruff", ["ruff", "check", "."]),
            ("5.2", "mypy (strict)", [sys.executable, "-m", "mypy"]),
            ("5.3", "pytest (warnings are errors)", [sys.executable, "-m", "pytest", "-q", "-x", "--deselect", "tests/test_smoke_script.py"]),
        ]
        out_dir = self.tmp / "dist"
        steps += [("5.4", "python -m build", [sys.executable, "-m", "build", "--outdir", str(out_dir)])]
        for sid, title, argv in steps:
            if shutil.which(argv[0]) is None and argv[0] != sys.executable:
                self.check(sid, title, True, skip=f'{argv[0]} not installed (pip install -e ".[dev]")')
                continue
            p = self.run(argv, cwd=ROOT, timeout=1800, env={**os.environ, "PYTHONUTF8": "1"})
            self.check(sid, title, p.code == 0, p.both[-600:])
        ver = self.run([sys.executable, "-c", "import db_lens; print(db_lens.__version__)"], cwd=ROOT).out.strip()
        rel = self.run([sys.executable, "scripts/release_check.py", f"v{ver}", "--dist", str(out_dir)], cwd=ROOT)
        self.check("5.6", "release gate: tag, pyproject, __version__, CHANGELOG and artifacts agree", rel.code == 0, rel.both[-300:])
        files = [str(x) for x in out_dir.glob("*")] if out_dir.exists() else []
        if files:
            p = self.run([sys.executable, "-m", "twine", "check", "--strict", *files], cwd=ROOT)
            self.check("5.5", "twine check --strict", p.code == 0 and p.both.count("PASSED") == len(files), p.both[-400:])

    def part7(self) -> None:
        print("Part 7: live servers")
        from sqlalchemy import make_url  # imported here so Parts 0-5 need no third-party package

        pg, my = os.environ.get("DBLENS_PG_URL"), os.environ.get("DBLENS_MYSQL_URL")
        if not (pg or my):
            self.check("7.0", "live servers", True, skip="set DBLENS_PG_URL and/or DBLENS_MYSQL_URL")
            return
        for label, url in (("pg", pg), ("my", my)):
            if not url:
                self.check(f"7.{label}", f"{label} server", True, skip="URL not set")
                continue
            u = make_url(url)
            env = {**self.env, "DBL_LIVE_PW": u.password or ""}
            dialect = u.get_backend_name()
            args = [
                "connect",
                "-D",
                dialect,
                "--host",
                u.host or "127.0.0.1",
                "-u",
                u.username or "",
                "-d",
                u.database or "",
                "--password-env",
                "DBL_LIVE_PW",
                "-n",
                f"live{label}",
            ] + (["--port", str(u.port)] if u.port else [])
            if u.drivername.split("+")[-1] != u.get_backend_name():
                args += ["--driver", u.drivername.split("+")[-1]]
            self.expect(
                f"7.{label}.1",
                f"{label}: structured connect with --password-env",
                self.cli(*args, env=env),
                has=("Connected",),
                hasnt=(u.password or "\0",),
            )
            t = self.cli("tables", "-p", f"live{label}", env=env)  # a fresh server may legitimately have no tables yet
            self.check(
                f"7.{label}.2",
                f"{label}: tables lists the schema (or says there are none)",
                t.code == 0 and ("table(s)" in t.out or "No tables found" in t.out),
                t.both[-300:],
            )
            self.expect(
                f"7.{label}.3",
                f"{label}: --strict refuses a write-capable admin",
                self.cli("query", "SELECT 1", "-p", f"live{label}", "--strict", env=env),
                code=1,
                has=("not verifiably read-only",),
            )
            self.expect(f"7.{label}.4", f"{label}: profile removed", self.cli("profiles", "remove", f"live{label}", "-y"), has=("Removed",))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--with-packaging", action="store_true", help="also run ruff, mypy, pytest, build, twine (Part 5)")
    ap.add_argument("--with-keyring", action="store_true", help="also test the real OS credential store (step 4.6)")
    ap.add_argument("--live", action="store_true", help="also test live servers from DBLENS_PG_URL / DBLENS_MYSQL_URL (Part 7)")
    ap.add_argument("--report", metavar="FILE", help="write a Markdown result table")
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="dbl-smoke-"))
    s = Smoke(tmp)
    started = time.time()
    print(f"db-lens smoke test | {platform.platform()} | Python {platform.python_version()} | temp dir {tmp}")
    try:
        s.part0()
        s.part1()
        s.part2()
        s.part3()
        s.part4(a.with_keyring)
        if a.with_packaging:
            s.part5()
        if a.live:
            s.part7()
    finally:
        if not a.keep:
            try:
                os.chmod(s.db, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            shutil.rmtree(tmp, ignore_errors=True)
    n = {k: sum(1 for r in s.results if r[2] == k) for k in ("PASS", "FAIL", "SKIP")}
    print(f"\n{n['PASS']} passed, {n['FAIL']} failed, {n['SKIP']} skipped in {time.time() - started:.0f}s")
    for sid, title, status, why in s.results:
        if status == "SKIP":
            print(f"  skipped {sid}: {title} ({why})")
    print("Manual-only (not covered here):\n  - " + "\n  - ".join(MANUAL))
    if a.report:
        lines = ["| Step | Result | Check | Notes |", "|---|---|---|---|"] + [
            f"| {i} | {st} | {t} | {s.ascii(w)[:120].replace('|', '/').replace(chr(10), ' ')} |" for i, t, st, w in s.results
        ]
        Path(a.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
