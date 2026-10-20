import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_check.py"
spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
rc = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(rc)  # type: ignore[union-attr]


@pytest.fixture
def tree(tmp_path: Path) -> Any:
    (tmp_path / "db_lens").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "db-lens"\nversion = "1.2.3"\n')
    (tmp_path / "db_lens" / "__init__.py").write_text('__version__ = "1.2.3"\n')
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## 1.2.3\nstuff\n\n## 1.2.2\nold\n")
    return tmp_path


def test_consistent_release_passes(tree: Any) -> None:
    assert rc.problems("v1.2.3", tree) == [] and rc.problems("1.2.3", tree) == []


@pytest.mark.parametrize("tag,needle", [("v1.2.4", "pyproject.toml version is 1.2.3"), ("vnext", "not a version"), ("v1.2", "not a version")])
def test_wrong_tag_is_caught(tree: Any, tag: Any, needle: Any) -> None:
    assert any(needle in p for p in rc.problems(tag, tree))


def test_each_source_is_checked_independently(tree: Any) -> None:
    (tree / "db_lens" / "__init__.py").write_text('__version__ = "1.2.0"\n')
    assert [p for p in rc.problems("v1.2.3", tree) if "__version__" in p]
    (tree / "CHANGELOG.md").write_text("## 1.2.2\n")
    assert [p for p in rc.problems("v1.2.3", tree) if "CHANGELOG" in p]
    (tree / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n')
    assert [p for p in rc.problems("v1.2.3", tree) if "pyproject" in p]


def test_prerelease_versions(tree: Any) -> None:
    for f, t in (
        ("pyproject.toml", 'version = "1.3.0rc1"\n'),
        ("db_lens/__init__.py", '__version__ = "1.3.0rc1"\n'),
        ("CHANGELOG.md", "## 1.3.0rc1\n"),
    ):
        (tree / f).write_text(t)
    assert rc.problems("v1.3.0rc1", tree) == []


def test_built_artifacts_must_carry_the_version(tree: Any, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    assert any("expected one wheel and one sdist" in p for p in rc.problems("v1.2.3", tree, dist))
    (dist / "db_lens-1.2.3-py3-none-any.whl").write_text("")
    (dist / "db_lens-1.2.3.tar.gz").write_text("")
    assert rc.problems("v1.2.3", tree, dist) == []
    (dist / "db_lens-1.2.3.tar.gz").rename(dist / "db_lens-1.2.2.tar.gz")
    assert any("does not carry version 1.2.3" in p for p in rc.problems("v1.2.3", tree, dist))


def test_the_real_repository_is_release_consistent() -> None:
    from db_lens import __version__

    assert rc.problems(f"v{__version__}") == []


def test_cli_exit_codes(tree: Any) -> None:
    ok = subprocess.run([sys.executable, str(SCRIPT), "v1.2.3", "--root", str(tree)], capture_output=True, text=True)
    bad = subprocess.run([sys.executable, str(SCRIPT), "v9.9.9", "--root", str(tree)], capture_output=True, text=True)
    assert ok.returncode == 0 and "release check OK" in ok.stdout and bad.returncode == 1 and "FAILED" in bad.stdout


def test_unpinned_actions_are_caught_and_pinned_ones_pass(tree: Any) -> None:
    wf = tree / ".github" / "workflows"
    wf.mkdir(parents=True)
    sha = "a" * 40
    (wf / "ci.yml").write_text(
        f"jobs:\n  x:\n    steps:\n      - uses: actions/checkout@{sha} # v4\n      - uses: ./local\n      - uses: actions/setup-python@v5\n"
    )
    found = rc.unpinned_actions(tree)
    assert found == ["ci.yml: action actions/setup-python@v5 is not pinned to a commit SHA"]
    assert any("setup-python" in p for p in rc.problems("v1.2.3", tree))
    (wf / "ci.yml").write_text(f"      - uses: actions/checkout@{sha}\n")
    assert rc.unpinned_actions(tree) == []


def test_tool_versions_must_match_everywhere(tree: Any) -> None:
    (tree / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\ndev = ["ruff==0.17.1", "mypy==2.4.0"]\n')
    (tree / "requirements-dev.txt").write_text("ruff==0.17.1\nmypy==2.4.0\n")
    (tree / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.17.1\n  - repo: https://github.com/pre-commit/mirrors-mypy\n    rev: v2.4.0\n"
    )
    assert rc.tool_drift(tree) == []
    (tree / "requirements-dev.txt").write_text("ruff==0.16.0\nmypy==2.4.0\n")
    (tree / ".pre-commit-config.yaml").write_text("repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.17.0\n")
    found = rc.tool_drift(tree)
    assert any("does not pin ruff==0.17.1" in p for p in found) and any("ruff-pre-commit rev is not v0.17.1" in p for p in found)
    assert rc.tool_drift(tree / "nowhere") == []


def test_the_real_repository_has_no_drift_and_pinned_actions() -> None:
    root = SCRIPT.parent.parent
    assert rc.unpinned_actions(root) == [] and rc.tool_drift(root) == []


def test_stale_versions_in_user_docs_are_caught(tree: Any) -> None:
    (tree / "README.md").write_text("Install 1.2.3 now. SQLAlchemy 2.1.2.30 is fine, requires 11.2.2x.\n")
    (tree / "SECURITY.md").write_text("fixed in 1.2.2\n")
    (tree / "TESTING.md").write_text("version v1.2.2 and 1.2.3\n")
    found = rc.stale_versions(tree, "1.2.3")
    assert found == ["SECURITY.md mentions stale version 1.2.2 (current is 1.2.3)", "TESTING.md mentions stale version 1.2.2 (current is 1.2.3)"]
    assert rc.stale_versions(tree / "nowhere", "1.2.3") == []
    assert any("stale version" in p for p in rc.problems("v1.2.3", tree))


def test_testing_md_must_match_source_count_and_cli_commands(tree: Any) -> None:
    (tree / "tests").mkdir()
    (tree / "tests" / "test_a.py").write_text("")
    (tree / "db_lens" / "cli.py").write_text('@app.command()\ndef query() -> None: ...\n\n\n@app.command(name="x")\ndef gui() -> None: ...\n')
    assert rc.source_file_count(tree) == 3
    (tree / "TESTING.md").write_text("Success: no issues found in 9 source files. Run db-lens query.\n")
    found = rc.doc_drift(tree)
    assert "TESTING.md says 9 source files, the repository has 3" in found and "TESTING.md never mentions the CLI command 'gui'" in found
    (tree / "README.md").write_text("no window docs\n")
    assert "README.md does not document `db-lens gui`" in rc.doc_drift(tree)
    (tree / "TESTING.md").write_text("no issues found in 3 source files; db-lens query; db-lens gui\n")
    (tree / "README.md").write_text("run `db-lens gui`\n")
    assert rc.doc_drift(tree) == []


GOOD_WF = "name: x\non: [push]\npermissions:\n  contents: read\njobs:\n  a:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n      - run: echo hi\n"


def test_workflow_yaml_schema(tree: Any) -> None:
    wf = tree / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(GOOD_WF)
    assert rc.yaml_problems(tree) == []
    bad = "name: x\njobs:\n  a:\n    runs-on: u\n    steps:\n      - name: nothing\n  b: {}\n"
    (wf / "ci.yml").write_text(bad)
    found = " | ".join(rc.yaml_problems(tree))
    for needle in ("no 'on' trigger", "no top-level 'permissions'", "timeout-minutes", "needs exactly one of", "needs 'runs-on' or 'uses'"):
        assert needle in found, found
    (wf / "ci.yml").write_text("jobs: [unclosed\n")
    assert "not valid YAML" in rc.yaml_problems(tree)[0]
    (wf / "ci.yml").write_text("- a list\n")
    assert "expected a mapping" in " ".join(rc.yaml_problems(tree))
    (wf / "ci.yml").write_text("name: x\non: push\npermissions: {}\njobs: {}\n")
    assert "no jobs" in " ".join(rc.yaml_problems(tree))
    (wf / "ci.yml").write_text(
        GOOD_WF.replace("steps:\n      - run: echo hi\n", "uses: ./.github/workflows/other.yml\n").replace(
            "runs-on: ubuntu-latest\n    timeout-minutes: 5\n    ", ""
        )
    )
    assert rc.yaml_problems(tree) == []


def test_dependabot_and_precommit_yaml_schema(tree: Any) -> None:
    (tree / ".github").mkdir()
    (tree / ".github" / "dependabot.yml").write_text(
        "version: 2\nupdates:\n  - package-ecosystem: pip\n    directory: /\n    schedule: { interval: weekly }\n"
    )
    (tree / ".pre-commit-config.yaml").write_text("repos:\n  - repo: https://x/y\n    rev: v1\n    hooks: [{ id: a }]\n")
    assert rc.yaml_problems(tree) == []
    (tree / ".github" / "dependabot.yml").write_text("version: 1\n")
    (tree / ".pre-commit-config.yaml").write_text("repos:\n  - repo: https://x/y\n    hooks: [{ name: a }]\n")
    found = " | ".join(rc.yaml_problems(tree))
    assert "needs version: 2" in found and "every repo needs" in found
    (tree / ".pre-commit-config.yaml").write_text("repos: []\n")
    assert "no repos" in " | ".join(rc.yaml_problems(tree))


def test_yaml_validation_reports_a_missing_pyyaml(tree: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    (tree / ".pre-commit-config.yaml").write_text("repos: []\n")
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert rc.yaml_problems(tree) == ["PyYAML is required to validate the YAML files (pip install pyyaml)"]


def test_default_tag_is_the_pyproject_version(tree: Any) -> None:
    out = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tree)], capture_output=True, text=True, check=False)
    assert out.returncode == 0 and "v1.2.3 matches" in out.stdout


def test_skips_are_only_allowed_in_live_marked_modules(tree: Any) -> None:
    (tree / "tests").mkdir()
    (tree / "tests" / "test_live.py").write_text(
        "import pytest\npytestmark = pytest.mark.live\n\n@pytest.mark.skipif(True, reason='x')\ndef test_a(): pytest.skip('x')\n"
    )
    (tree / "tests" / "test_clean.py").write_text("def test_b(): pass\n")
    assert rc.skip_problems(tree) == []
    (tree / "tests" / "test_bad.py").write_text(
        "import pytest\n\n@pytest.mark.skipif(True, reason='x')\ndef test_a(): pytest.skip('x')\n\ndef test_c():\n    pytest.importorskip('x')\n\n@pytest.mark.skip\ndef test_d(): ...\n"
    )
    found = rc.skip_problems(tree)
    assert [f.split(" ")[1] for f in found] == ["pytest.mark.skipif", "pytest.skip", "pytest.importorskip", "pytest.mark.skip"]
    assert all(f.startswith("tests/test_bad.py:") for f in found) and any("skipif" in p for p in rc.problems("v1.2.3", tree))


def test_text_that_merely_mentions_skips_is_not_a_skip(tree: Any) -> None:
    (tree / "tests").mkdir()
    (tree / "tests" / "test_t.py").write_text("DOC = 'use pytest.skip() or skipif here'\n# pytest.mark.skip\n")
    assert rc.skip_problems(tree) == []


def test_pragma_no_cover_is_forbidden_in_the_guarded_modules(tree: Any) -> None:
    for name in rc.GUARDED_MODULES:
        (tree / "db_lens" / name).write_text("x = 1  # pragma: no cover\ny = 2  #pragma:no cover - lock\nz = 3\n")
    found = rc.pragma_problems(tree)
    assert len(found) == 2 * len(rc.GUARDED_MODULES) and found[0] == "db_lens/store.py:1: '# pragma: no cover' is not allowed in this module"
    (tree / "db_lens" / "store.py").write_text("x = 1\n")
    assert len(rc.pragma_problems(tree)) == 2 * (len(rc.GUARDED_MODULES) - 1)
    assert any("pragma" in p for p in rc.problems("v1.2.3", tree))
    assert rc.pragma_problems(tree / "nowhere") == []


def test_the_real_repository_has_no_skips_outside_live_and_no_guarded_pragmas() -> None:
    root = SCRIPT.parent.parent
    assert rc.skip_problems(root) == [] and rc.pragma_problems(root) == []


def _gate_wf(extra_needs: str = "", drop: str = "") -> str:
    steps = [f"      - run: {g}\n" for g in rc.GATES if g != drop]
    return f"name: x\non: push\npermissions: {{}}\njobs:\n  validate:\n    runs-on: u\n    timeout-minutes: 5\n    steps:\n{''.join(steps)}  build:\n{extra_needs}    runs-on: u\n    timeout-minutes: 5\n    steps:\n      - run: echo build\n"


def test_ci_and_release_must_run_every_gate(tree: Any) -> None:
    wf = tree / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(_gate_wf())
    (wf / "release.yml").write_text(_gate_wf("    needs: validate\n"))
    assert rc.gate_problems(tree) == []
    (wf / "ci.yml").write_text(_gate_wf(drop="python scripts/smoke_test.py"))
    assert rc.gate_problems(tree) == ["ci.yml: no step runs the gate 'python scripts/smoke_test.py'"]
    assert any("smoke_test" in p for p in rc.problems("v1.2.3", tree))


def test_release_build_must_wait_for_the_validation_job(tree: Any) -> None:
    wf = tree / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "release.yml").write_text(_gate_wf())
    assert rc.gate_problems(tree) == ["release.yml: the build job does not need the validation job (validate)"]
    (wf / "release.yml").write_text(_gate_wf("    needs: [validate]\n"))
    assert rc.gate_problems(tree) == []
    (wf / "release.yml").write_text(_gate_wf("    needs: validate\n", drop="pytest"))
    found = rc.gate_problems(tree)
    assert "release.yml: no step runs the gate 'pytest'" in found and "release.yml: no single job runs every gate before the build" in found


def test_gate_check_ignores_missing_or_broken_workflows(tree: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert rc.gate_problems(tree) == []
    wf = tree / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("jobs: [unclosed\n")
    assert rc.gate_problems(tree) == []  # yaml_problems reports it
    (wf / "ci.yml").write_text("- not a mapping\n")
    assert len(rc.gate_problems(tree)) == len(rc.GATES)
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert rc.gate_problems(tree) == []


def test_the_real_workflows_run_every_gate() -> None:
    assert rc.gate_problems(SCRIPT.parent.parent) == []
