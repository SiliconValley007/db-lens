# Contributing to db-lens

## Set up

```bash
git clone <your fork> db-lens && cd db-lens
python -m venv .venv && . .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev,drivers]"
```

## Before you open a pull request

```bash
ruff check . && ruff format --check .   # lint + format
mypy                              # strict types for db_lens/, tests/ and scripts/
pytest -q                         # unit + mock suites; coverage >= 98.5 %; warnings are errors
coverage json -o coverage.json && python scripts/coverage_gate.py   # every module >= 96 %
DBLENS_SIMULATE_OS=windows pytest -q   # also =posix: the other platform's helper branches, executed on your machine
python scripts/smoke_test.py      # the unattended TESTING.md
```

- Add a test for every behaviour change. Anything touching a database engine needs a **live** test in `tests/test_integration.py`
  (marker `live`, run with `pytest -m live --no-cov`) **and** an offline equivalent registered in `EQUIVALENTS` of `tests/test_live_equivalents.py`; Oracle changes are covered by `tests/test_oracle_mock.py` and must say so in the PR.
- Never log, print or store a secret. The `server_must_survive` fixture must stay on: no teardown may call a method that can stop a server.
- Keep `README.md` (Verification table), `TESTING.md` and `CHANGELOG.md` in step with the change.

## Releasing (maintainers)

1. Bump `version` in `pyproject.toml` and `__version__` in `db_lens/__init__.py`, and add a `## X.Y.Z` section to `CHANGELOG.md`.
2. `python scripts/release_check.py vX.Y.Z` must pass locally (it also checks SHA-pinned actions and identical tool versions; refresh pins with Dependabot).
3. One-time on pypi.org: _Account → Publishing → add a trusted publisher_ for this repository, workflow `release.yml`, environment `pypi`
   (and the same on test.pypi.org with environment `testpypi` to rehearse via _Actions → Release → Run workflow → testpypi_). No token is stored.
4. `git tag vX.Y.Z && git push --tags`. The workflow checks the versions, builds, runs the built wheel on Linux, Windows and macOS, publishes to
   PyPI and creates the GitHub release with the artifacts.

## Reporting bugs

Run `python scripts/smoke_test.py --report result.md` and attach the table (passwords redacted) to the issue.
