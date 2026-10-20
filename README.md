# 🔍 db-lens

**Understand any SQL database in 10 seconds.** A terminal schema browser and _guarded_ query runner for SQLite, PostgreSQL,
MySQL/MariaDB and Oracle: tables with row/size metrics, full schema trees, and read-only queries with table, JSON or CSV output.

```bash
git clone <your fork or the upstream URL> db-lens && cd db-lens
pip install ".[drivers]"           # core + PostgreSQL, MySQL/MariaDB, Oracle drivers + keyring. SQLite needs nothing extra
# or pick: pip install ".[postgres,secure]"   (extras: secure postgres mysql mysqlclient mysql-connector oracle ssh gui drivers)
db-lens --version
```

Python 3.10+. Pinned/bounded dependencies are in `pyproject.toml` and `requirements.txt` (see [Verification](#verification)).
**Before trusting a copy, run [`TESTING.md`](TESTING.md)** (manual plan) or `python scripts/smoke_test.py` (the same plan, unattended).

## Quickstart

```bash
# 1. Save a connection profile (the last one connected becomes the default)
db-lens connect 'postgresql+psycopg2://me@db.example.com/shop' --ask-password -n prod   # password prompt -> OS keyring
db-lens connect sqlite:///C:/data/local.db -n local
db-lens profiles                              # name, masked URI, schema, secret storage, TLS, account type
db-lens profiles use local                    # change the default
db-lens profiles rename prod production       # keeps the keyring password and the default flag
db-lens profiles remove production            # deletes a profile + its keyring secret (asks first; -y skips)

# 2. Explore
db-lens tables -s user                        # columns, rows (~ = catalog estimate), size; --search filters, -S picks a schema
db-lens inspect orders -n 5                   # columns, PK/FK, indexes, constraints, partitions + 5 sample rows

# 3. Query (read-only)
db-lens query "SELECT * FROM users LIMIT 10"
db-lens query "SELECT id, email FROM users" -o json -l 500 | jq .        # -o table|json|csv, -l row limit, -t timeout seconds
echo "SELECT count(*) FROM orders" | db-lens query -p prod -               # stdin;  -f file.sql also works
db-lens query "PRAGMA table_info(users)" -p local                         # read-only SQLite pragmas are allowed

# 4. Scripting
db-lens tables -o json | jq '.[] | select(.rows > 1000000)'      # tables and profiles also speak -o json / -o csv
db-lens profiles -o csv > profiles.csv                          # (no passwords; CSV cells that look like formulas are neutralised)
db-lens -v tables                                               # --verbose: what db-lens is doing, on stderr (never SQL text or secrets)
db-lens --install-completion                                    # tab completion for bash, zsh, fish, PowerShell (also completes profile names)
```

### Connecting without shell-quoting trouble

Passwords and URLs full of `$ & ; ! % @` are painful to quote. Build the connection from options instead (values are escaped by
SQLAlchemy, never by the shell), or feed the URI in from somewhere the shell does not parse:

```bash
db-lens connect -D postgresql --host db.example.com --port 5432 -d shop -u me --ask-password -n prod
db-lens connect -D mysql --driver mysqldb --host db -d shop -u me --password-env SHOP_DB_PW      # stored as a reference, never the value
db-lens connect -D oracle --host ora.example.com --port 1521 -u scott --ask-password --option service_name=ORCLPDB1
db-lens connect -D sqlite -d C:\data\shop.db                              # relative paths are stored as absolute paths
db-lens connect --uri-env DATABASE_URL -n ci                              # or --uri-file secrets/db.url, or:  ... | db-lens connect -
echo $PW | db-lens connect -D postgresql --host h -d d -u me --password-stdin
```

| Option                                                                                 | Meaning                                                                                                                                                       |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-D/--dialect`, `--driver`                                                             | `sqlite`, `postgresql`, `mysql`, `mariadb`, `oracle`; driver override (`psycopg2`, `pymysql`, `mysqldb`, `mysqlconnector`, `oracledb`, …)                     |
| `--host --port -u/--user -d/--database`                                                | Server parts (`-d` is the file path for SQLite)                                                                                                               |
| `--ask-password` / `--password-env NAME` / `--password-stdin`                          | Hidden prompt / environment variable _name_ / first line of stdin. A password typed inside a URI works too, but is visible in shell history, so db-lens warns |
| `--ssl-mode disable\|require\|verify-ca\|verify-full`, `--ssl-ca --ssl-cert --ssl-key` | TLS, translated per driver (see below)                                                                                                                        |
| `--option KEY=VALUE` (repeatable)                                                      | Any other driver option (secret-looking keys are refused)                                                                                                     |
| `--uri-env NAME`, `--uri-file PATH`, `-`                                               | Where to read a ready-made URI from                                                                                                                           |

**TLS is verified, not assumed.** Drivers differ in their defaults (PyMySQL 1.2 encrypts on its own, 1.1 does not; mysqlclient never
does), so after connecting db-lens asks the server whether the session is really encrypted. A profile created with
`--ssl-mode require|verify-ca|verify-full` (or `?sslmode=…` for PostgreSQL) **refuses to run any command over an unencrypted
connection**, on every run, and `profiles` shows the demanded level. TLS flags work for PostgreSQL and MySQL/MariaDB (PyMySQL, mysqlclient,
mysql-connector); for Oracle use `--option protocol=tcps` (not verifiable by db-lens).

### SSH tunnels

Databases behind a bastion host are reached through your normal OpenSSH setup (keys, `ssh-agent`, `known_hosts`, `~/.ssh/config`): db-lens
starts `ssh -N -L` for each command and stops it afterwards. No extra Python package is needed; the `ssh` client ships with Windows 10+, macOS and Linux.

```bash
db-lens connect -D postgresql --host db.internal --port 5432 -d shop -u me --ask-password --ssh deploy@bastion.example.com:22 --ssh-key ~/.ssh/id_ed25519 -n prod
db-lens connect -D mysql --host db.internal -d shop -u me --ask-password --ssh deploy@bastion --ssh-accept-new -n staging   # trust the bastion's key on first contact only
```

The tunnel never prompts (`BatchMode`): unknown or changed host keys, wrong keys and refused forwards fail with a clear message instead of hanging;
password logins to the bastion are not supported. `--host` is the database host **as the bastion sees it**. For PostgreSQL the real host name is kept for
certificate checks (`sslmode=verify-full` works; only the address is redirected, via libpq `hostaddr`); for MySQL/MariaDB/Oracle use `verify-ca`
or `require`, because the client connects to `127.0.0.1`. Not applicable to SQLite.

### Profile configuration

| Secret form in the URI                     | Where the password lives                                                                                                 |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `user:pw@host` (default)                   | OS keyring (needs `db-lens[secure]`); falls back to the `0600` config file with a warning. `--plaintext` forces the file |
| `user:${ENV_VAR}@host` or `--password-env` | Nowhere: resolved from the environment on every run. Works inside a password (`pre-${A}-post`); `$$` is a literal `$`    |

Profiles are stored in `~/.db-lens/config.json` (override with the `DB_LENS_CONFIG` environment variable); writes are atomic and
locked, a corrupt file is reported and never overwritten, and `profiles remove` also deletes the keyring entry.

## Safety model: read this before pointing it at production

**The SQL check is a convenience, not a security boundary.** It parses the statement (sqlglot) and rejects writes, DDL,
`SELECT … INTO`, locking reads, admin functions and `EXPLAIN ANALYZE`, but it **cannot see inside user-defined functions or
stored procedures**: `SELECT run_mutation_job()` parses as a harmless SELECT.

Real protection needs both of:

1. **A dedicated read-only database role**: `GRANT SELECT` only; no superuser; no `EXECUTE` on routines that write.
2. **An engine-level read-only session**, which db-lens enables _and verifies_ before your SQL runs (SQLite `query_only`,
   PostgreSQL read-only transaction, MySQL/MariaDB `READ ONLY`, Oracle `SET TRANSACTION READ ONLY`). If it cannot be confirmed,
   nothing runs.

### `--strict`

```bash
db-lens query "SELECT * FROM users" --strict
```

Refuses to run unless **all** of these hold: the account is verifiably read-only (including roles reachable via `SET ROLE` or
unresolved roles), it can execute **no user-created routines** (extension/standard functions are informational and do not block),
and the statement calls no user-created or unrecognised functions. Use it in scripts and CI. Without `--strict`, db-lens still
warns on stderr when the account can write or when a statement calls a possible UDF. `connect` audits the account the same way
(user-created routines are listed apart from standard/extension functions).

Other protections: database content is sanitised before printing (no terminal-escape injection, Rich markup is never
interpreted); **CSV export neutralises spreadsheet formulas** (text starting with `= + - @ TAB CR` gets a leading `'`, headers
included; `--csv-raw` disables) and uses `\n` line endings so Windows does not double them; errors are redacted of passwords; MySQL/MariaDB
`--limit` drops the connection instead of reading the rest of a huge result. See [`SECURITY.md`](SECURITY.md) to report a vulnerability.

## Architecture

```
db_lens/
  cli.py       Typer commands + Rich rendering, error panels (guard), output formats, --strict policy, profiles sub-commands
  conn.py      connection URLs from structured options, per-driver TLS mapping
  db.py        engines, verified read-only sessions, TLS check, schema resolution, catalog metrics, account probe, MySQL early stop
  sqlguard.py  sqlglot AST validation (single statement, no writes/DDL/INTO/locks/admin functions), UDF call classification
  store.py     profiles: atomic + locked config, keyring / ${ENV} secrets, error redaction
  tunnel.py    SSH tunnels via the system OpenSSH client
scripts/smoke_test.py   unattended version of TESTING.md (Windows, Linux, macOS)
scripts/release_check.py  release gate: tag = pyproject = __version__ = CHANGELOG = built artifacts
tests/                  unit + mock suites (SQLite, fakes) and live-server integration tests
```

## Verification

What ran against a **live server** and what did not, stated plainly:

| Engine                                                                              | Status                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SQLite                                                                              | Fully tested (unit + CLI end-to-end)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| PostgreSQL 16 (psycopg2)                                                            | **Live-server tested**: read-only session, statement timeout (SQLSTATE 57014), transaction-poisoning recovery, catalog estimates, streaming cursors, account/role/routine probing, error codes, TLS (`require`/`disable`, post-connect verification)                                                                                                                                                                                                                                                                                                    |
| MariaDB 10.11 (PyMySQL, mysqlclient, mysql-connector pure-Python _and_ C-extension) | **Live-server tested**: read-only session, `max_statement_time` timeout (via both `mysql://` and `mariadb://` URLs), early-stop teardown timing and server-side abort for each driver, role/routine probing, error codes, TLS `require`/`disable`/`verify-ca`/`verify-full` per driver incl. wrong-CA refusal                                                                                                                                                                                                                                           |
| SSH tunnels (OpenSSH 9.6)                                                           | **Live tested** against a real `sshd` to PostgreSQL and MariaDB: traffic is forwarded by sshd (checked in its log), wrong key / dead server / unknown host key (never trusted) fail cleanly, and no `ssh` child process is left behind. Windows' `ssh.exe` is untested                                                                                                                                                                                                                                                                                  |
| Oracle's MySQL Server 5.7 / 8.x / 9.x                                               | **Not tested on a live server.** It shares the MariaDB code path, but these differences are unverified: the read-only variable name (`transaction_read_only` vs `tx_read_only`; both are tried and the flag must read back as on), the timeout (`max_execution_time`, milliseconds, SELECT only, vs MariaDB's `max_statement_time`), role discovery (MariaDB's `information_schema.applicable_roles` may be missing: roles found in `SHOW GRANTS` are then reported as _unresolved_, never assumed safe), `SHOW GRANTS` formatting and routine metadata |
| **Oracle Database**                                                                 | **Mock-tested only.** Session statements, catalog tiers, case-folding, privilege probes and error paths are verified against scripted fakes (`tests/fakes.py`, `tests/test_oracle_mock.py`), which check statement text, bind parameters, ordering and failure handling. **The Oracle SQL has never run on a real Oracle server.** Validate it in a non-production database before relying on it                                                                                                                                                        |

**Server-survival guard.** Every live test runs under an autouse fixture that records MySQL/MariaDB `Uptime` and `Com_shutdown`
and the PostgreSQL postmaster start time, and fails if the server restarted or received a shutdown command. It exists because
an early release's teardown called mysqlclient's `shutdown()` (= `mysql_shutdown()`) and stopped the database. Teardown now calls a
raw-connection method only for an exact (driver, class) pair, see `SOCKET_CLOSERS` in `db.py`; everything else is the
driver's ordinary `close()`.

**Static typing.** `mypy --strict` passes on `db_lens/`, `tests/` and `scripts/`; `ruff check` and `ruff format --check` are clean (all run in CI).

**Offline equivalents.** Every live-server test (`tests/test_integration.py`, marker `live`) has a mock equivalent in `tests/test_live_equivalents.py` (checked by a meta-test) so the default suite proves the same statements, ordering, fail-closed paths and cleanup without a server. The mocks do not prove real server behaviour; the live tests do.

**Secrets.** Passwords never appear in errors, verbose logs or tracebacks (`store.redact`, no locals in tracebacks, `db_lens.cli:run`); config, lock and export files are created `0600` on POSIX.

**Dependency bounds** in `pyproject.toml` / `requirements.txt` are the oldest versions the suite was run against and the latest
tested; the full suite (warnings are errors, nothing skipped) passes on both stacks against the live servers:

|                                | Oldest                           | Latest tested                     |
| ------------------------------ | -------------------------------- | --------------------------------- |
| SQLAlchemy                     | 2.0.30                           | 2.1.x                             |
| PyMySQL / mysqlclient          | 1.1.0 / 2.2.0                    | 1.2.3 / 2.3.0                     |
| mysql-connector-python         | 9.5.0 (C extension)              | 26.7.0 (pure Python)              |
| psycopg2-binary                | 2.9.9                            | 2.9.x                             |
| typer / click / rich / sqlglot | 0.16.0 / 8.2.1 / 13.7.0 / 25.0.0 | 0.27.x / 8.5.0 / 15.0.0 / 30.21.0 |

`oracledb` is only checked for the `call_timeout` attribute it relies on.

## Development

```bash
pip install -e ".[dev,drivers]"
ruff check . && ruff format --check . && mypy && pytest -q   # lint, format, strict types, unit + mock suites (coverage >= 98.5 %, warnings are errors)
python scripts/coverage_gate.py                # after `coverage json -o coverage.json`: every module >= 96 %
python scripts/smoke_test.py                   # unattended TESTING.md (add --with-packaging to include lint/types/tests/build)
DBLENS_PG_URL=postgresql+psycopg2://admin:pw@host/db \
DBLENS_MYSQL_URL=mysql+pymysql://admin:pw@host/db pytest -m live --no-cov -q   # live-server tests (admin accounts: they create test roles)
```

Live tests are deselected by default (marker `live`); with `-m live` the ones whose URL is unset skip (`DBLENS_MYSQL_TLS_CA` adds the `verify-*` TLS tests). GUI tests need Tk and a display (Linux: `python3-tk`; a private `Xvfb` is started automatically when installed). See
[`CONTRIBUTING.md`](CONTRIBUTING.md). `.github/workflows/ci.yml` runs the suites on Linux and Windows; `.github/workflows/release.yml` publishes tagged releases (see CONTRIBUTING).

## Desktop window: `db-lens gui`

```
pip install "db-lens[gui]"        # adds the sv-ttk theme; Tk itself ships with Python (Linux: apt install python3-tk)
db-lens gui                       # opens on the default profile
db-lens gui -p shop               # or a saved profile
```

Pick a profile, type one statement, press **Ctrl+Enter** (or F5). It runs through exactly the checks of `db-lens query`: the SQL guard, a verified read-only
session, the **Limit** and **Timeout** boxes, and **Strict** (= `--strict`). Results are a sortable grid (right-click to copy a cell or rows); **CSV…** and
**JSON…** export what is shown (atomic, `0600`, spreadsheet formulas neutralised). The **Log** button shows redacted activity; **Theme** switches light/dark;
size, theme, limit, timeout and the last queries are remembered in `~/.db-lens/gui.json`. Queries run on a worker thread, so the window never freezes.
Without a display (an ssh session) the command exits with a clear message instead of a traceback. The building blocks are importable too:
`db_lens.gui.widgets` (`DataGrid`, `LogPane`, `Banner`, `SearchEntry`, `StatusBar`), `db_lens.gui.settings`, `db_lens.gui.theme` and `db_lens.service`.

## License

MIT, see `LICENSE`.
