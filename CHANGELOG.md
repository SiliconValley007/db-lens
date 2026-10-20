# Changelog

## 0.7.1

Release-readiness and hardening pass. No behaviour change for valid input.

### Security

- Uncaught failures can no longer print a traceback or local variables (which could contain a password): both Typer apps set
  `pretty_exceptions_show_locals=False` and the console entry point is now `db_lens.cli:run`, which prints a redacted one-line panel.
- Redaction (`store.redact`) now also covers a raw `@` inside a URL password, `password=`/`pwd:`/`token`/`api_key` pairs (including JSON style)
  and the `quote_plus` form of every registered secret. Verbose log records drop `exc_info`/`stack_info`. A password read from stdin or
  `--ask-password` is registered before the URI is parsed, so even a URI parse error cannot echo it.
- Config hardening: the lock file is created `0600`; a world/group-readable `config.json` is made `0600` when loaded (POSIX); config and
  GUI preferences are read/written as UTF-8; profile records of the wrong shape are rejected as a corrupt config (never overwritten).

### Added

- **`db-lens gui`**: a desktop window (`db_lens/gui/app.py`) built from the widgets, settings, theme and service modules. It runs statements through
  the same guard / read-only session / limit / timeout / `--strict` path as `db-lens query` on a worker thread, exports CSV/JSON, remembers
  preferences and history, and shows redacted errors and activity. `db-lens gui -p NAME` picks a profile; no display or no Tk gives a clear panel.
- `db_lens.service` (cell display, atomic formula-safe CSV/JSON export with `0600` files) and `db_lens.gui.theme`, the two modules the
  GUI widgets (`db_lens.gui.widgets`, `db_lens.gui.settings`) imported but that were missing; `pip install "db-lens[gui]"`.
- `pytest` works out of the box on headless Linux: `tests/conftest.py` starts a private Xvfb when `$DISPLAY` is unset (a test proves it).
- Windows parity: POSIX-only branches in `store.py` are excluded from coverage by `# pragma: no cover`, POSIX-permission tests skip on Windows, and
  CI runs a `windows-latest` unit job (Python 3.10 and 3.13) with lint, strict mypy, the suite and the coverage gate.
- Offline mock equivalents for every live-server test (`tests/test_live_equivalents.py`); a meta-test fails if a live test has none.
- `scripts/coverage_gate.py` (total >= 98.5 %, every module >= 96 %); `scripts/release_check.py` also verifies SHA-pinned workflow actions,
  identical exact tool versions across `pyproject.toml`, `requirements-dev.txt` and `.pre-commit-config.yaml`, no stale version strings in
  README/SECURITY/TESTING, the mypy source-file count and CLI commands quoted in TESTING, and the structure of the workflow, dependabot and
  pre-commit YAML. It now defaults the tag to the pyproject version.

### Testing and portability

- OS-specific code is now in small helpers (`store._is_windows`, `_load_fcntl`, `_open_lockfile`, `_locked`, `_fsync_dir`, `_tighten`; `tunnel._creation_flags`),
  every `# pragma: no cover` is gone from `store.py`/`db.py`/`tunnel.py`, and BOTH the POSIX and the Windows path of each helper is unit-tested on any platform
  (monkeypatched syscalls, a fake `fcntl`). No test is skipped on Windows or Linux: permission assertions are platform-aware (`tests.fakes.assert_mode`).
- GUI tests no longer need Xvfb: with no `$DISPLAY` and no `Xvfb` (or when `tkinter.Tk()` raises `TclError`, or `DBLENS_TK_STUB=1`) `tests/conftest.py`
  installs an in-process Tk stand-in (`tests/tkstub.py`) and the same GUI tests run on it. `tests/test_headless.py` proves the three paths.
- Every POSIX helper branch is exercised with `_is_windows -> False` plus mocked POSIX syscalls (fake `fcntl`, `os.open`/`os.fsync` on directory
  descriptors, `os.O_DIRECTORY`), every Windows branch with `_is_windows -> True`, so `windows-latest` still executes 100 % of both. `DBLENS_SIMULATE_OS=windows|posix pytest`
  runs the whole suite with the other platform's defaults (CI-proof on any OS); `tests.conftest` fixtures `as_posix` / `as_windows` flip one test.
- `tests/conftest.py` picks the GUI backend with `DBLENS_TK_BACKEND=auto|real|stub` (`DBLENS_TK_STUB=1` is an alias for `stub`); Windows and macOS use their native Tk without
  `$DISPLAY`/Xvfb, and `tests/test_headless.py` covers real Tk, the stub and the platform logic of the selector on every OS.
- `release.yml` runs a `validate` job with exactly the CI gates before it builds or publishes; `release_check.py` asserts `ci.yml` and `release.yml` run every gate.
- Coverage gates raised: total >= 98.5 %, every module >= 96 %.
- `scripts/release_check.py` fails on any `pytest.skip` / `skipif` / `importorskip` outside live-marked modules and on `# pragma: no cover` in
  `store.py`, `db.py` or `tunnel.py`.

### Changed

- Live-server tests carry the `live` marker and are deselected by default (`pytest -m live --no-cov` runs them) instead of being skipped.
- `python -m db_lens` and the `db-lens` script print identical help (`run()` passes `prog_name="db-lens"`; a test compares them byte for byte).
- `mypy --strict` now covers `tests/` as well, with no relaxations (untyped SQLAlchemy dialect constructors go through `tests.fakes.new_dialect`); `ruff format --check` is enforced; line endings are LF (`.gitattributes`).
- CI/release workflows: actions pinned to commit SHAs, least-privilege `permissions`, `timeout-minutes`, concurrency, build provenance
  attestation, PEP 740 publish attestations. Dev tools are pinned exactly (ruff 0.17.1, mypy 2.4.0, pytest 9.1.1).

## 0.7.0

First release prepared for publication.

### Fixed (during release verification)

- mysql-connector C-extension rejected string TLS switches from a URL (`argument must be bool`); they are passed as real booleans.
- **mysqlclient: early `--limit` stop could shut down the database server.** The driver-agnostic teardown called
  `connection.shutdown()` on any driver that had one; on mysqlclient that is `mysql_shutdown()`. It is now called only on a genuine
  mysql-connector connection. Regression tests cover every driver name, and the live-server tests assert server uptime.
- `connect` probed the account twice (two connections); it now probes once.
- **mysql-connector did not stream at all** (every `--limit` downloaded the whole table): SQLAlchemy 2.0.x lacks the unbuffered
  cursor method, and the C-extension connector ignores `cursor(buffered=False)` while the connection is marked buffered. Both are
  handled; the early stop for the C-extension class closes the C handle (`mysql_close`), never `shutdown`.
- **`--timeout` was silently ignored on MariaDB reached through a `mysql://` URL** (MySQL's `max_execution_time` does not exist
  there). The server flavour is now detected; verified live with `SLEEP()`.
- **MySQL role discovery could silently find nothing** where `information_schema.applicable_roles` is missing; roles named in
  `SHOW GRANTS` are now reported as unresolved (blocking under `--strict`).
- Click >= 8.2.1 is required: older Click mixes stderr into stdout, and 8.2.0 cannot complete parameters of nested sub-commands (shell completion of profile names).
- PyMySQL `MySQLResult.__del__` "Exception ignored" noise after an early stop.

### Added

- `profiles rename OLD NEW` (moves the keyring password, keeps order and default).
- `-o json|csv` for `tables` and `profiles` (stable field names, no secrets, formula-safe CSV).
- `--verbose/-v` (or `DB_LENS_VERBOSE=1`): redacted diagnostics on stderr; never SQL text or secrets.
- Shell completion (`--install-completion`, `--show-completion`), including saved profile names.
- SSH tunnels: `connect --ssh [user@]host[:port] [--ssh-key FILE] [--ssh-accept-new]` through the system OpenSSH client (BatchMode, host keys verified, PostgreSQL certificate checks keep the real host name).
- Release workflow (`.github/workflows/release.yml`): version gate, built-wheel tests on Linux/Windows/macOS, PyPI trusted publishing, GitHub release; `scripts/release_check.py`.
- `db-lens profiles use NAME` and `profiles remove NAME [--yes]` (also removes the keyring password; `profiles list` is the default).
- Connecting without shell-quoting trouble: `connect -D/--dialect --host --port -u -d`, `--ask-password`, `--password-env`,
  `--password-stdin`, `--uri-env`, `--uri-file`, `-` (URI on stdin), `--option KEY=VALUE`; secret-looking options are refused.
- TLS: `--ssl-mode disable|require|verify-ca|verify-full`, `--ssl-ca/--ssl-cert/--ssl-key`, mapped per driver (PostgreSQL, PyMySQL,
  mysqlclient, mysql-connector) and **verified on the live connection**. A profile that demands TLS refuses to run unencrypted.
- `db-lens --version`; `DB_LENS_CONFIG` environment variable to relocate the config file.
- `mypy --strict` over `db_lens/` and `scripts/`.
- `scripts/smoke_test.py`: the manual plan in `TESTING.md` as an unattended script (Windows, Linux, macOS), also run by `pytest`.
- `TESTING.md` (manual Windows 10/11 test plan), `SECURITY.md`, `CONTRIBUTING.md`, issue/PR templates, Dependabot, a Windows CI job.
- Account audit: user-created routines (blocking under `--strict`) are reported apart from extension/standard functions
  (informational); unresolved roles are reported rather than ignored.
- Graceful Oracle degradation: a refused `ALTER SESSION SET CURRENT_SCHEMA` (ORA-01031) or unsupported timeout becomes a
  structured notice. The READ ONLY session never degrades.
- Normalised error codes (`ORA-xxxxx`, `MY-nnnn`, `SQLSTATE-xxxxx`) in error panels.
- Version bounds for all dependencies and drivers, verified at the lower bound and the latest versions (see README).

### Changed

- Relative SQLite paths are stored as absolute paths (a profile no longer breaks after `cd`).
- `profiles` is now a command group (`db-lens profiles` still lists).

### Removed

- Unused helpers (`write_capability`, `account_risks`, `unknown_calls`, `_row`).

## 0.1 - 0.6

Internal iterations: AST read-only guard, verified read-only sessions, keyring/ENV secrets, atomic config, CSV formula
neutralisation, catalog fallbacks, multi-driver early abort, Oracle mock suite.
