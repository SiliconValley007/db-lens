# Testing & QA Guide: db-lens v0.7.1

**Platform Target:** Windows 10/11 (PowerShell / Windows Terminal) | **Last Updated:** October 2026 | **Package Version:** 0.7.1

## Overview & Safety Caveats

This document is the end-to-end manual test plan for validating db-lens on Windows. Anyone who clones the `db-lens` repository can
follow it to confirm the project works on their machine **before trusting it with a real database**. It needs **no database
software**: Parts 0-6 run against a SQLite file that a script creates. Part 7 (optional) adds real PostgreSQL / MariaDB servers.
Allow about 45 minutes by hand, or about a minute with the scripted run below.

> [!WARNING]
> **Platform caveat and scope.** Development and baseline validation happened on Linux, against live PostgreSQL and MariaDB servers.
> Windows-specific behaviour (file attributes, file replacement under antivirus locks, console glyphs, the pager, the Credential
> Manager) is exactly what this checklist tests; a failure there is a genuine finding, not a mistake in the guide.
> **Oracle Database** (mock-tested only) and **Oracle's MySQL Server** (never run) cannot be verified by this guide.
> "All steps pass" means "works as documented on your machine", not "safe for any database": read the README _Safety model_ first.

## Pre-Flight Check

Run every command in **PowerShell 7+ or Windows PowerShell 5.1**, preferably inside **Windows Terminal**. Command Prompt quotes
differently and is not covered. The old `conhost` window may show boxes instead of the symbols `✔ ★ 🔑 →`: that is the font, not a failure.
Mark each step in the **Status** column (☐ → PASS / FAIL). A Python traceback anywhere, instead of a red error panel, is a bug.

**Fast path (unattended).** After Part 0, the scripted version of Parts 0-4 runs the same checks and prints PASS / FAIL per step:

```powershell
python scripts\smoke_test.py                       # Parts 0-4 on a throw-away SQLite database (isolated config; your profiles are untouched)
python scripts\smoke_test.py --with-packaging --with-keyring --report smoke.md     # + Part 5 and step 4.6; writes a result table
```

It exits `0` only when every executed step passed. **Not covered by the script (do these by hand):** step 3.6 (Excel), the interactive
password prompt (4.25), the tab-completion install (4.30), Part 6 (GitHub) and Part 7 (live servers and SSH; `--live` automates a subset).

---

## Part 0: Environment Setup

| ID  | Command / Action                                                                                                                                                        | Expected Result                                                                                                                                                                                                       | Status |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 0.1 | Install Python 3.12 (3.10-3.13 are supported) from python.org and tick **Add python.exe to PATH**. Open PowerShell and run `python --version`                           | `Python 3.12.x`                                                                                                                                                                                                       | ☐      |
| 0.2 | `cd` into the repository folder `db-lens` (the one containing `pyproject.toml`). From a ZIP instead: `cd ~; Expand-Archive db-lens.zip .; cd db-lens`. Run `dir -Force` | Contains `db_lens`, `scripts`, `tests`, `.github`, `README.md`, `TESTING.md`, `SECURITY.md`, `CONTRIBUTING.md`, `LICENSE`, `.gitignore`, `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`, `CHANGELOG.md` | ☐      |
| 0.3 | `python -m venv .venv` then `.\.venv\Scripts\Activate.ps1`                                                                                                              | Prompt starts with `(.venv)`. If blocked: `Set-ExecutionPolicy -Scope Process Bypass`, then retry                                                                                                                     | ☐      |
| 0.4 | `$env:PYTHONUTF8 = "1"`                                                                                                                                                 | Silent. Keeps piped output UTF-8 (set it again in every new window)                                                                                                                                                   | ☐      |
| 0.5 | `pip install -e ".[secure,dev]"`                                                                                                                                        | Ends `Successfully installed … db-lens-0.7.1 …` with no red errors                                                                                                                                                    | ☐      |
| 0.6 | `db-lens --help`                                                                                                                                                        | Lists `connect`, `profiles`, `tables`, `inspect`, `query`, `gui`                                                                                                                                                      | ☐      |
| 0.7 | `python -m db_lens --help` and `db-lens --version`                                                                                                                      | Same help as 0.6; version prints `db-lens 0.7.1`                                                                                                                                                                      | ☐      |

## Part 1: Provision the Local SQLite Test Database

No database server is needed. Create the script (a single-quoted here-string, so PowerShell changes nothing inside it) and run it:

```powershell
Set-Content -Path make_db.py -Encoding utf8 -Value @'
import sqlite3, pathlib
p = pathlib.Path.home() / "dbl_test" / "shop.db"
p.parent.mkdir(exist_ok=True)
p.unlink(missing_ok=True)
c = sqlite3.connect(p)
c.executescript("""
CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, note TEXT);
CREATE TABLE parent(a INT, b INT, PRIMARY KEY(a,b));
CREATE TABLE "order items"(id INTEGER PRIMARY KEY, "unit price" REAL NOT NULL CHECK("unit price">=0), pa INT, pb INT, sku TEXT UNIQUE, FOREIGN KEY(pa,pb) REFERENCES parent(a,b));
CREATE INDEX ix_sku2 ON "order items"(sku,"unit price");
CREATE VIEW v_users AS SELECT id,name FROM users;
INSERT INTO parent VALUES (1,2);
INSERT INTO "order items" VALUES (1,9.5,1,2,'s1');
INSERT INTO users VALUES (1,'Ann','=1+1'),(2,'Bob','-5'),(3,NULL,'@SUM(A1)'),(4,'[red]bold[/red]','+1 555'),(5,'x'||char(27)||'[31mEVIL',char(9)||'tab'),(6,'NULL',''),(7,'x','plain');
""")
c.commit()
c.close()
'@
python make_db.py
$db = "$HOME\dbl_test\shop.db"; $u = "sqlite:///" + ($db -replace '\\','/')
```

**Expected:** `python make_db.py` prints nothing; `Test-Path $db` returns `True`. `$u` is used in every later step.

## Part 2: Core Inspection Commands

| ID   | Command                                 | Expected Result                                                                                                                                                                                                                                               | Status |
| ---- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 2.1  | `db-lens connect $u -n shop`            | `✔ Connected shop sqlite:///C:/Users/…/shop.db · secret: none`. No warning panel                                                                                                                                                                              | ☐      |
| 2.2  | `db-lens profiles`                      | One row `shop ★`; Secret `none`; TLS `—`; Account `WRITE-CAPABLE` (the file is writable)                                                                                                                                                                      | ☐      |
| 2.3  | `db-lens tables`                        | Title `sqlite · main · 3 table(s)`. Rows: `order items` (5 columns, 1 row), `parent` (2, 1), `users` (3, 7). The view is **not** listed. Dim footer `N pages × 4,096 B …`                                                                                     | ☐      |
| 2.4  | `db-lens tables -s USER`                | Only `users`                                                                                                                                                                                                                                                  | ☐      |
| 2.5  | `db-lens tables -s zzz`                 | `No tables found.`                                                                                                                                                                                                                                            | ☐      |
| 2.6  | `db-lens tables -S nope; $LASTEXITCODE` | Red panel `Schema 'nope' not found. Available: main`, then `1`                                                                                                                                                                                                | ☐      |
| 2.7  | `db-lens inspect "order items" -n 5`    | **Columns:** `id` 🔑 PK; `unit price` NOT NULL; `pa` → `parent.a`; `pb` → `parent.b`; `sku`. **Indexes:** `ix_sku2 (sku, unit price)`. **Constraints:** `UNIQUE (sku)` and a `CHECK` on unit price (the word "None" never appears). A `Sample (1 rows)` table | ☐      |
| 2.8  | `db-lens inspect parent`                | Both `a` and `b` marked 🔑 PK                                                                                                                                                                                                                                 | ☐      |
| 2.9  | `db-lens inspect v_users`               | Columns `id`, `name`                                                                                                                                                                                                                                          | ☐      |
| 2.10 | `db-lens inspect nope; $LASTEXITCODE`   | Red panel "not found", then `1`                                                                                                                                                                                                                               | ☐      |
| 2.11 | `db-lens inspect users -n 3 -S MAIN`    | Works: schema names match case-insensitively; `Sample (3 rows)`                                                                                                                                                                                               | ☐      |

## Part 3: Query Execution, Formatting & Safety Rules

| ID   | Command                                                                                                                     | Expected Result                                                                                                                                                                                                  | Status |
| ---- | --------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 3.1  | `db-lens query "SELECT id,name FROM users ORDER BY id"`                                                                     | 7 rows. Row 3: dim italic `NULL`. Row 4: `[red]bold[/red]` printed literally, **not** coloured. Row 5: visible `\x1b[31mEVIL`, no colour change, no beep. Row 6: the plain text `NULL`. Footer `7 row(s) · N ms` | ☐      |
| 3.2  | `db-lens query "SELECT * FROM users" -l 3`                                                                                  | Footer `3 row(s) (truncated; raise --limit)`                                                                                                                                                                     | ☐      |
| 3.3  | `$j = db-lens query "SELECT id,name FROM users" -o json \| Out-String \| ConvertFrom-Json; $j.Count; $j[2].name -eq $null`  | `7`, then `True`. The JSON is parseable (the `N row(s)` statistics go to stderr, not into the data)                                                                                                              | ☐      |
| 3.4  | `db-lens query "SELECT note FROM users" -o csv`                                                                             | Header `note`, then `'=1+1`, `'-5`, `'@SUM(A1)`, `'+1 555`, a tab-prefixed value starting with `'`, `""`, `plain` (unchanged)                                                                                    | ☐      |
| 3.5  | `db-lens query "SELECT note FROM users" -o csv --csv-raw`                                                                   | `=1+1`, `-5`, … **without** the apostrophe                                                                                                                                                                       | ☐      |
| 3.6  | `db-lens query "SELECT note FROM users" -o csv \| Out-File n.csv -Encoding utf8`, then open `n.csv` in Excel or LibreOffice | No cell shows a computed result (`2`, `#NAME?`). Cells are plain text, one row per line, **no blank rows in between**                                                                                            | ☐      |
| 3.7  | `"SELECT count(*) AS n FROM users" \| db-lens query -`                                                                      | `n = 7`                                                                                                                                                                                                          | ☐      |
| 3.8  | `Set-Content q.sql "SELECT 1 AS one;"; db-lens query -f q.sql`                                                              | `one = 1`                                                                                                                                                                                                        | ☐      |
| 3.9  | `db-lens query "PRAGMA table_info(users)"`                                                                                  | Column list (`id`, `name`, `note`)                                                                                                                                                                               | ☐      |
| 3.10 | `$long = "a" * 300; db-lens query "SELECT 1 AS c, '$long' AS wide" --max-cell 20`                                           | The `wide` value is cut with `…` after 20 characters; the column name is complete                                                                                                                                | ☐      |
| 3.11 | `db-lens query "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c" -t 1`                 | Within about 2 s a red panel containing `interrupted` and `→ Timed out (raise --timeout).` Exit 1                                                                                                                | ☐      |
| 3.12 | `db-lens query "SELECT my_fn(1)"`                                                                                           | Yellow `! calls my_fn: if user-defined…`, then a red panel `no such function: my_fn`. Exit 1                                                                                                                     | ☐      |
| 3.13 | `db-lens query "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 200) SELECT x FROM c" -l 200`            | 200 rows. In a short window the output opens in the pager (`more`): Space pages, `q` quits. With `--no-pager` it prints straight through. Either is fine; report a crash                                         | ☐      |

### Read-Only SQL Safety Enforcer

Run each statement with `db-lens query "<statement>"; $LASTEXITCODE`:

1. `DELETE FROM users`
2. `DROP TABLE users`
3. `INSERT INTO users VALUES (9,'z','z')`
4. `UPDATE users SET name='z'`
5. `SELECT 1; DROP TABLE users`
6. `WITH a AS (SELECT 1) DELETE FROM users`
7. `PRAGMA user_version=5`
8. `SELECT load_extension('x')`
9. an empty statement: `"" \| db-lens query -`

**Expected Behavior:** every one of 1-8 produces a red panel titled `UnsafeSQL` and exit code `1`. Number 9 produces a red panel
(`Empty statement.`) and exit code `1`.

| ID   | Command                                           | Expected Result                               | Status |
| ---- | ------------------------------------------------- | --------------------------------------------- | ------ |
| 3.14 | `db-lens query "SELECT count(*) AS n FROM users"` | Still `n = 7`: nothing above changed the data | ☐      |

## Part 4: Strict Mode, Security, Secrets & Profiles

| ID   | Command / Action                                                                                                                                                                                                                | Expected Result                                                                                                                                                                                                         | Status |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 4.1  | `db-lens query "SELECT 1 AS x" --strict; $LASTEXITCODE`                                                                                                                                                                         | Red panel `--strict: not verifiably read-only (database file is writable by this OS user)`, exit `1`                                                                                                                    | ☐      |
| 4.2  | `attrib +R $db`, repeat 4.1, then `attrib -R $db`                                                                                                                                                                               | The query runs and shows `x = 1`. If it still refuses, record it as a Windows finding (this check relies on `os.access`)                                                                                                | ☐      |
| 4.3  | `Get-Content $HOME\.db-lens\config.json`                                                                                                                                                                                        | JSON with profile `shop`, its `uri` and `"writable": true`. **No password anywhere**                                                                                                                                    | ☐      |
| 4.4  | `db-lens connect "postgresql://bob:Sup3rSecretPw@127.0.0.1:1/x" -n fake`                                                                                                                                                        | A red panel (driver missing or connection refused). The text `Sup3rSecretPw` appears **nowhere**. `db-lens profiles` does not list `fake`                                                                               | ☐      |
| 4.5  | `$env:DBL_PW = "envSecret99"; db-lens connect 'postgresql://bob:${DBL_PW}@127.0.0.1:1/x' -n fake2`                                                                                                                              | Red panel (connection failure). `envSecret99` appears nowhere. `profiles` does not list `fake2`                                                                                                                         | ☐      |
| 4.6  | `python -c "import keyring; keyring.set_password('dbl-test','u','v'); print(keyring.get_password('dbl-test','u')); keyring.delete_password('dbl-test','u')"`                                                                    | Prints `v` (Windows Credential Manager works). If it errors, db-lens falls back to a `${ENV}` reference or the config file, with a warning                                                                              | ☐      |
| 4.7  | `Set-Content $HOME\.db-lens\config.json "{broken"; db-lens profiles; $LASTEXITCODE`                                                                                                                                             | Red panel `Config file … is corrupt … Not modified`, then `1`. The file still contains `{broken`                                                                                                                        | ☐      |
| 4.8  | `Remove-Item $HOME\.db-lens\config.json; db-lens connect $u -n shop`                                                                                                                                                            | Works again (the profile is recreated)                                                                                                                                                                                  | ☐      |
| 4.9  | `Get-ChildItem $HOME\.db-lens -Force`                                                                                                                                                                                           | `config.json` and no leftover `.config-*.tmp` files (Linux/macOS also keep a `config.lock`; Windows does not)                                                                                                           | ☐      |
| 4.10 | `db-lens connect -D sqlite -d $db -n two; db-lens connect -D sqlite -d $db -n three`                                                                                                                                            | Both `✔ Connected`; the last one connected (`three`) is the default                                                                                                                                                     | ☐      |
| 4.11 | `db-lens profiles use shop`                                                                                                                                                                                                     | `Default profile is now shop.`; `profiles` shows `shop ★`                                                                                                                                                               | ☐      |
| 4.12 | `db-lens profiles remove two` and answer `n`                                                                                                                                                                                    | Prompt `Remove profile 'two'? [y/N]`, then `Aborted!`; exit `1`; `two` is still listed                                                                                                                                  | ☐      |
| 4.13 | `db-lens profiles remove two --yes`                                                                                                                                                                                             | `✔ Removed two.`; `two` is gone                                                                                                                                                                                         | ☐      |
| 4.14 | `db-lens profiles remove zzz -y; $LASTEXITCODE`                                                                                                                                                                                 | Red panel `Profile 'zzz' not found (saved: …)`, then `1`                                                                                                                                                                | ☐      |
| 4.15 | `db-lens profiles remove three -y; db-lens profiles remove shop -y`                                                                                                                                                             | Removing the default while several remain prints `No default profile; choose one with db-lens profiles use NAME`. Removing down to one profile makes it the default. Last removal prints `✔ Removed shop.`              | ☐      |
| 4.16 | `db-lens tables; $LASTEXITCODE` (now no profiles)                                                                                                                                                                               | Red panel `Profile None not found. Run: db-lens connect <uri>`, then `1`                                                                                                                                                | ☐      |
| 4.17 | `db-lens connect -D sqlite -d $db -n s`                                                                                                                                                                                         | `✔ Connected s`: no URI, nothing to quote                                                                                                                                                                               | ☐      |
| 4.18 | `$env:DBL_URI = $u; db-lens connect --uri-env DBL_URI -n ue`                                                                                                                                                                    | `✔ Connected ue`                                                                                                                                                                                                        | ☐      |
| 4.19 | `Set-Content uri.txt $u; db-lens connect --uri-file uri.txt -n uf`                                                                                                                                                              | `✔ Connected uf`                                                                                                                                                                                                        | ☐      |
| 4.20 | `$u \| db-lens connect - -n ui`                                                                                                                                                                                                 | `✔ Connected ui`                                                                                                                                                                                                        | ☐      |
| 4.21 | `"Sup3r`$ecret!" \| db-lens connect -D postgresql --host 127.0.0.1 --port 1 -u bob -d x --password-stdin`                                                                                                                       | Red connection error; `Sup3r$ecret!` appears **nowhere**; nothing saved                                                                                                                                                 | ☐      |
| 4.22 | `db-lens connect -D sqlite -d $db --ssl-mode require`                                                                                                                                                                           | Red panel `--ssl-* flags are not supported for sqlite …`; exit `1`                                                                                                                                                      | ☐      |
| 4.23 | `db-lens connect -D postgresql --host h --option password=x`                                                                                                                                                                    | Red panel `looks like a secret`; exit `1`                                                                                                                                                                               | ☐      |
| 4.24 | `Get-Content $HOME\.db-lens\config.json`                                                                                                                                                                                        | Contains `s`, `ue`, `uf`, `ui`; **not** `fake`, `fake2` or any password                                                                                                                                                 | ☐      |
| 4.25 | `db-lens connect -D postgresql --host 127.0.0.1 --port 1 -u bob -d x --ask-password` and type a password                                                                                                                        | The typed characters are **not echoed**; then a red connection error that does not contain the password                                                                                                                 | ☐      |
| 4.26 | `db-lens profiles rename ui renamed`, then `db-lens profiles rename renamed s` and `db-lens tables -p renamed`                                                                                                                  | `✔ Renamed ui to renamed.`; renaming onto an existing name fails with `already exists` (exit `1`); the renamed profile works                                                                                            | ☐      |
| 4.27 | `db-lens profiles -o json \| Out-String \| ConvertFrom-Json` and `db-lens profiles list -o csv`                                                                                                                                 | Parseable JSON (one entry has `default: true`; fields `name uri schema secret tls ssh account risks`, no password); CSV with a header row                                                                               | ☐      |
| 4.28 | `db-lens tables -p renamed -o json \| Out-String \| ConvertFrom-Json` and `db-lens tables -p renamed -o csv -s user`                                                                                                            | JSON array with `table`, `columns`, `rows`, `rows_estimated`, `size_bytes`; `users` has `rows 7`, `columns 3`. CSV header `table,columns,rows,rows_estimated,size_bytes`                                                | ☐      |
| 4.29 | `db-lens -v query "SELECT 'secret-literal' AS s" -p renamed`                                                                                                                                                                    | On stderr lines starting `db-lens [debug]` (profile, session statements, read-only check). The SQL text `secret-literal` is **not** in the diagnostics. Without `-v` there are no `[debug]` lines                       | ☐      |
| 4.30 | `db-lens --install-completion` (then open a new PowerShell window), type `db-lens profiles use ` and press Tab                                                                                                                  | Saved profile names are offered. (`db-lens --show-completion` prints the script without installing it)                                                                                                                  | ☐      |
| 4.31 | `db-lens connect -D postgresql --host h --ssh -oProxyCommand=x`, then `db-lens connect -D sqlite -d $db --ssh bastion`, then `db-lens connect -D postgresql --host h --port 5432 -u u -d d --ssh nobody@127.0.0.1:1 -n sshfail` | Red panels: `--ssh expects [user@]host[:port]`; `--ssh does not apply to SQLite`; `TunnelError … Connection refused` (needs the Windows OpenSSH client; if `ssh` is missing the panel says so). Exit `1`, nothing saved | ☐      |

## Part 5: Automated Test Suite & Release Packaging

| ID  | Command                                                                            | Expected Result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Status |
| --- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --- |
| 5.1 | `pip install -e ".[dev,drivers]"` then `ruff check . && ruff format --check .`     | `All checks passed!` and `files already formatted`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | ☐      |
| 5.2 | `mypy`                                                                             | `Success: no issues found in 40 source files`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | ☐      |
| 5.3 | `pytest -q -rs`                                                                    | **Zero failures and zero warnings** (warnings are errors in `pyproject.toml`). The unattended script also runs here (about 30 s). Live-server tests are _deselected_ (marker `live`, reported as `deselected`) and **nothing is skipped on any platform**: permission assertions are platform-aware and the POSIX/Windows branches are tested through monkeypatched helpers. GUI tests run on real Tk (own display or a private Xvfb) or, when neither exists, on the in-process stub (`tests/tkstub.py`). Choose the backend with `DBLENS_TK_BACKEND=auto | real   | stub` (`DBLENS_TK_STUB=1`=`stub`; `real`never falls back); Windows and macOS use their native Tk and need no`$DISPLAY`or Xvfb. Run the whole suite with the other platform's defaults via`DBLENS_SIMULATE_OS=windows pytest`or`=posix`(CI job`os-symmetry`) | ☐   |
| 5.4 | `python -m build`                                                                  | `Successfully built db_lens-0.7.1.tar.gz and db_lens-0.7.1-py3-none-any.whl`, no warnings                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | ☐      |
| 5.5 | `twine check --strict dist/*`                                                      | `PASSED` for both files                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | ☐      |
| 5.6 | `pip install --force-reinstall --no-deps (Get-Item dist\*.whl); db-lens --version` | The built wheel installs and runs: `db-lens 0.7.1`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | ☐      |
| 5.7 | `python scripts\release_check.py v0.7.1 --dist dist`                               | `release check OK: v0.7.1 matches pyproject.toml, __version__, CHANGELOG and the built artifacts`                                                                                                                                                                                                                                                                                                                                                                                                                                                          | ☐      |

## Part 6: Git & GitHub Pipeline Readiness

| ID  | Action                                                                                                         | Expected Result                                                                                                                                                                                                      | Status |
| --- | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 6.1 | `git status --short` after Parts 0-5 (clean up with `Remove-Item make_db.py,q.sql,uri.txt,n.csv`)              | Nothing untracked. No `.venv`, `dist`, `build`, `__pycache__`, `*.egg-info`, `.pytest_cache`, `.mypy_cache`, `*.zip` or `*.db`                                                                                       | ☐      |
| 6.2 | Check the `LICENSE` holder and add `[project.urls]` to `pyproject.toml` (publisher's own repository `db-lens`) | MIT text with your name; URLs point to your `db-lens` repository                                                                                                                                                     | ☐      |
| 6.3 | Push to GitHub and open the repository page                                                                    | README and TESTING tables, code blocks and the warning callout render; the Verification section says Oracle is mock-only                                                                                             | ☐      |
| 6.4 | Open the **Actions** tab                                                                                       | `unit` green on Python 3.10 / 3.11 / 3.12 / 3.13; `windows` green (its artifact `windows-smoke-report` lists every step); `packaging` green. `integration` runs live PostgreSQL + MariaDB; if it fails, send the log | ☐      |
| 6.5 | Open **Security → Report a vulnerability**                                                                     | Private reporting is enabled (enable it under _Settings → Code security_ if not), matching `SECURITY.md`                                                                                                             | ☐      |

## Part 7: Optional Live Engine Integration Testing

Use **throw-away** servers only: the tests create and drop roles, users and tables.

1. Install PostgreSQL (EDB installer) and/or MariaDB (MSI) and remember the admin passwords. In MariaDB create a database: `CREATE DATABASE test;`
2. Run the live suite:

```powershell
# Set database connections (this PowerShell window only)
$env:DBLENS_PG_URL    = "postgresql+psycopg2://postgres:YOUR_PASSWORD@127.0.0.1/postgres"
$env:DBLENS_MYSQL_URL = "mysql+pymysql://root:YOUR_PASSWORD@127.0.0.1/test"

# Execute the integration suite
pip install -e ".[dev,drivers]"
pytest -m live -q -rs --no-cov
python scripts\smoke_test.py --live            # optional: the same plan against the live servers
```

**Expected:** no failures, and both servers still running afterwards (every live test is guarded by a server-uptime check). The `mysqlclient`
tests are skipped unless you also `pip install mysqlclient` (it needs a compiler on some Python versions); the TLS `verify-*` tests need
`$env:DBLENS_MYSQL_TLS_CA` to point to your server's CA file.

| ID  | Manual Interactive Verification                                                                                                                                                                                                                    | Expected Result                                                                                                                                                                                                       | Status |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 7.1 | `db-lens connect "postgresql+psycopg2://postgres@127.0.0.1/postgres" --ask-password -n pg`                                                                                                                                                         | `secret: keyring` (or a plaintext warning if the keyring fails; then open Credential Manager → Windows Credentials and look for `db-lens`) and a yellow **Write-capable account** panel (a superuser)                 | ☐      |
| 7.2 | `db-lens tables -p pg`, then `db-lens inspect <table> -p pg -n 3`                                                                                                                                                                                  | Tables with `~` row estimates and sizes; the schema tree with a sample                                                                                                                                                | ☐      |
| 7.3 | `db-lens query "SELECT 1" -p pg --strict; $LASTEXITCODE`                                                                                                                                                                                           | **Must refuse** (`not verifiably read-only`), exit `1`. Repeat with a `SELECT`-only role: `--strict` then runs                                                                                                        | ☐      |
| 7.4 | `db-lens connect -D postgresql --host 127.0.0.1 -u postgres -d postgres --ask-password --ssl-mode require -n pgtls` (PostgreSQL started with `ssl = on`)                                                                                           | `TLS: require (verified)`; `profiles` shows `require`. With the server's `ssl = off` it must fail with a TLS error rather than connect in plaintext                                                                   | ☐      |
| 7.5 | `db-lens profiles remove pg -y; db-lens profiles remove pgtls -y`                                                                                                                                                                                  | Both removed; the Credential Manager entry for each is deleted                                                                                                                                                        | ☐      |
| 7.6 | With a bastion you can `ssh` into (check first: `ssh user@bastion echo ok`): `db-lens connect -D postgresql --host DB_AS_SEEN_FROM_BASTION --port 5432 -u USER -d DB --ask-password --ssh user@bastion -n viassh`, then `db-lens tables -p viassh` | `✔ Connected viassh … via ssh user@bastion`; `tables` works; afterwards `Get-Process ssh` shows **no** leftover tunnel process. An unknown host key fails with `--ssh-accept-new` advice instead of waiting for input | ☐      |

---

## Reporting Results

Copy this table into an issue, mark each Part PASS / FAIL and attach the output of any FAIL (passwords redacted). The scripted run
produces most of it for you: `python scripts\smoke_test.py --with-packaging --report smoke.md`.

| Part                                   | Result | Notes                                                        |
| -------------------------------------- | ------ | ------------------------------------------------------------ |
| 0 Setup                                |        | Windows build (`winver`), Python version, PowerShell version |
| 1-2 Connect / browse                   |        |                                                              |
| 3 Queries and safety                   |        |                                                              |
| 4 Strict / config / secrets / profiles |        |                                                              |
| 5 Suite and packaging                  |        | `pytest` summary line                                        |
| 6 Repo hygiene                         |        |                                                              |
| 7 Live servers (optional)              |        | Server versions                                              |

**Pass criteria:** every step in Parts 0-5 PASS, with no Python traceback anywhere (errors must be red panels). That shows the SQLite
path, CLI, profile and secret handling, and packaging work on your machine. It does **not** cover Oracle Database or Oracle's MySQL Server.

## Troubleshooting

| Symptom                                              | Fix                                                                   |
| ---------------------------------------------------- | --------------------------------------------------------------------- |
| `db-lens` is not recognised                          | The venv is not active (0.3), or use `python -m db_lens …`            |
| `UnicodeEncodeError` when piping                     | Run `$env:PYTHONUTF8="1"` (0.4)                                       |
| `pip` cannot build a package                         | Use Python 3.12 from python.org; skip the optional `mysqlclient`      |
| Boxes instead of symbols                             | Use Windows Terminal; cosmetic only                                   |
| Antivirus locks `shop.db` or the config              | Add `$HOME\dbl_test` and `$HOME\.db-lens` to the exclusions and retry |
| `smoke_test.py` fails with `No module named db_lens` | Run it inside the activated venv after `pip install -e .`             |
