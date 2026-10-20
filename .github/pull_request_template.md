## What and why

## Checklist
- [ ] `ruff check .`, `mypy`, `pytest -q` pass
- [ ] `python scripts/smoke_test.py` passes
- [ ] New behaviour has a test (live test for engine-specific code; Oracle changes say "mock only")
- [ ] README Verification table / TESTING.md / CHANGELOG updated if behaviour changed
- [ ] No secret is logged, printed or stored
