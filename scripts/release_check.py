#!/usr/bin/env python3
"""Release gate used by .github/workflows/release.yml: the git tag, pyproject version, db_lens.__version__ and the CHANGELOG must agree,
every workflow action must be pinned to a commit SHA, the exact tool versions must match across pyproject.toml, requirements-dev.txt and
.pre-commit-config.yaml, no stale version string may remain in README/SECURITY/TESTING, TESTING must name every CLI command and the right source-file
count, and the workflow / pre-commit / dependabot YAML must have the required structure.

    python scripts/release_check.py                        # tag = v<pyproject version>
    python scripts/release_check.py v0.7.1                 # before building
    python scripts/release_check.py v0.7.1 --dist dist     # after building: every artifact carries that version

Exit code 0 = consistent. Pure standard library (runs on Python 3.10+)."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+([abrc]+\d+|\.post\d+|\.dev\d+)?$")


PINNED = {"ruff": "ruff-pre-commit", "mypy": "mirrors-mypy"}
USES = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*([^\s#]+)")


def unpinned_actions(root: Path) -> list[str]:
    """Workflow steps whose action is not pinned to a full 40-hex commit SHA (a tag can be moved; a SHA cannot)."""
    found: list[str] = []
    for wf in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        for ref in USES.findall(wf.read_text(encoding="utf-8")):
            if not ref.startswith("./") and not re.search(r"@[0-9a-f]{40}$", ref):
                found.append(f"{wf.name}: action {ref} is not pinned to a commit SHA")
    return found


def tool_drift(root: Path) -> list[str]:
    """pyproject.toml (dev extra), requirements-dev.txt and .pre-commit-config.yaml must name the same exact tool versions."""
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return []
    py = pyproject.read_text(encoding="utf-8")
    pins = dict(re.findall(r'"([A-Za-z0-9_.-]+)==([^"]+)"', py))
    found: list[str] = []
    req = root / "requirements-dev.txt"
    if req.exists():
        text = req.read_text(encoding="utf-8")
        for name, ver in pins.items():
            if not re.search(rf"(?m)^{re.escape(name)}=={re.escape(ver)}\b", text):
                found.append(f"requirements-dev.txt does not pin {name}=={ver} (pyproject.toml)")
    hooks = root / ".pre-commit-config.yaml"
    if hooks.exists():
        text = hooks.read_text(encoding="utf-8")
        for tool, repo in PINNED.items():
            if tool in pins and not re.search(rf"{repo}\s*\n\s*rev:\s*v{re.escape(pins[tool])}\b", text):
                found.append(f".pre-commit-config.yaml: {repo} rev is not v{pins[tool]} (pyproject.toml pins {tool}=={pins[tool]})")
    return found


DOCS = ("README.md", "SECURITY.md", "TESTING.md")


def stale_versions(root: Path, version: str) -> list[str]:
    """Version strings of OLDER releases (the CHANGELOG headings) must not linger in the user-facing docs."""
    changelog = root / "CHANGELOG.md"
    if not changelog.exists():
        return []
    old = {v for v in re.findall(r"(?m)^## (\d+\.\d+\.\d+)\b", changelog.read_text(encoding="utf-8")) if v != version}
    found: list[str] = []
    for name in DOCS:
        path = root / name
        if path.exists():
            text = path.read_text(encoding="utf-8")
            found += [
                f"{name} mentions stale version {v} (current is {version})"
                for v in sorted(old)
                if re.search(rf"(?<![\d.]){re.escape(v)}(?![\d.])", text)
            ]
    return found


def source_file_count(root: Path) -> int:
    return sum(1 for d in ("db_lens", "tests", "scripts") for p in (root / d).rglob("*.py") if "__pycache__" not in p.parts)


def doc_drift(root: Path) -> list[str]:
    """TESTING.md must quote the real mypy source-file count and mention every top-level CLI command."""
    testing, cli = root / "TESTING.md", root / "db_lens" / "cli.py"
    found: list[str] = []
    if testing.exists():
        text = testing.read_text(encoding="utf-8")
        for n in re.findall(r"no issues found in (\d+) source files", text):
            if int(n) != source_file_count(root):
                found.append(f"TESTING.md says {n} source files, the repository has {source_file_count(root)}")
        if cli.exists():
            for cmd in re.findall(r"(?m)^@app\.command\([^)]*\)\ndef (\w+)\(", cli.read_text(encoding="utf-8")):
                if not re.search(rf"\b{cmd}\b", text):
                    found.append(f"TESTING.md never mentions the CLI command '{cmd}'")
    readme = root / "README.md"
    if readme.exists() and cli.exists() and "def gui(" in cli.read_text(encoding="utf-8") and "db-lens gui" not in readme.read_text(encoding="utf-8"):
        found.append("README.md does not document `db-lens gui`")
    return found


def _mapping(node: Any, where: str, found: list[str]) -> dict[Any, Any]:
    if not isinstance(node, dict):
        found.append(f"{where}: expected a mapping")
        return {}
    return node


def yaml_problems(root: Path) -> list[str]:
    """Structural schema checks for the GitHub workflows, dependabot.yml and .pre-commit-config.yaml (needs PyYAML)."""
    targets = sorted((root / ".github" / "workflows").glob("*.y*ml"))
    extra = [p for p in (root / ".github" / "dependabot.yml", root / ".pre-commit-config.yaml") if p.exists()]
    if not targets and not extra:
        return []
    try:
        import yaml
    except ImportError:
        return ["PyYAML is required to validate the YAML files (pip install pyyaml)"]
    found: list[str] = []
    for path in [*targets, *extra]:
        name = str(path.relative_to(root))
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            found.append(f"{name}: not valid YAML ({str(e).splitlines()[0]})")
            continue
        top = _mapping(doc, name, found)
        if path.parent.name == "workflows":
            if "on" not in top and True not in top:  # YAML 1.1 reads the key `on` as the boolean True
                found.append(f"{name}: no 'on' trigger")
            if "permissions" not in top:
                found.append(f"{name}: no top-level 'permissions' (default to least privilege)")
            jobs = _mapping(top.get("jobs"), f"{name} jobs", found)
            if not jobs:
                found.append(f"{name}: no jobs")
            for jid, job in jobs.items():
                job = _mapping(job, f"{name} job {jid}", found)
                if "runs-on" not in job and "uses" not in job:
                    found.append(f"{name} job {jid}: needs 'runs-on' or 'uses'")
                if "runs-on" in job and not (isinstance(job.get("steps"), list) and job["steps"]):
                    found.append(f"{name} job {jid}: no steps")
                if "runs-on" in job and not isinstance(job.get("timeout-minutes"), int):
                    found.append(f"{name} job {jid}: no integer 'timeout-minutes'")
                for i, step in enumerate(job.get("steps") or []):
                    if not (isinstance(step, dict) and ("uses" in step) != ("run" in step)):
                        found.append(f"{name} job {jid} step {i + 1}: needs exactly one of 'uses' / 'run'")
        elif path.name == "dependabot.yml":
            ups = top.get("updates")
            if (
                top.get("version") != 2
                or not isinstance(ups, list)
                or not ups
                or not all(isinstance(u, dict) and {"package-ecosystem", "directory", "schedule"} <= set(u) for u in ups)
            ):
                found.append(f"{name}: needs version: 2 and updates with package-ecosystem, directory and schedule")
        else:
            repos = top.get("repos")
            if not isinstance(repos, list) or not repos:
                found.append(f"{name}: no repos")
                continue
            for r in repos:
                r = _mapping(r, f"{name} repo", found)
                hooks = r.get("hooks")
                if not (
                    isinstance(r.get("repo"), str)
                    and isinstance(r.get("rev"), str)
                    and isinstance(hooks, list)
                    and hooks
                    and all(isinstance(h, dict) and "id" in h for h in hooks)
                ):
                    found.append(f"{name}: every repo needs string 'repo' and 'rev' and hooks with 'id' ({r.get('repo')})")
    return found


SKIP_NAMES = {"pytest.skip", "pytest.importorskip", "pytest.mark.skip", "pytest.mark.skipif"}
GUARDED_MODULES = ("store.py", "db.py", "tunnel.py")  # security / locking / OS-branch code: every path must be tested, none excluded


def skip_problems(root: Path) -> list[str]:
    """No skip of any kind outside modules marked live (``pytestmark = pytest.mark.live``): the default suite must report 0 skipped."""
    found: list[str] = []
    for path in sorted((root / "tests").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        marked = any(
            isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets) and "live" in ast.unparse(n.value)
            for n in tree.body
        )
        if marked:
            continue
        found += [
            f"tests/{path.name}:{n.lineno}: {ast.unparse(n)} outside a live-marked module"
            for n in sorted(ast.walk(tree), key=lambda x: (getattr(x, "lineno", 0), getattr(x, "col_offset", 0)))
            if isinstance(n, ast.Attribute) and ast.unparse(n) in SKIP_NAMES
        ]
    return found


def pragma_problems(root: Path) -> list[str]:
    """`# pragma: no cover` must not hide security, locking or OS-branch code: those modules are tested on both platforms via helpers + monkeypatch."""
    found: list[str] = []
    for name in GUARDED_MODULES:
        path = root / "db_lens" / name
        if path.exists():
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"#\s*pragma:\s*no cover", line):
                    found.append(f"db_lens/{name}:{i}: '# pragma: no cover' is not allowed in this module")
    return found


GATES = (
    "ruff check .",
    "ruff format --check .",
    "mypy --strict db_lens tests scripts",
    "pytest",
    "python scripts/coverage_gate.py",
    "python scripts/release_check.py",
    "python scripts/smoke_test.py",
)


def _run_text(job: Any) -> str:
    steps = job.get("steps") if isinstance(job, dict) else None
    return "\n".join(str(s["run"]) for s in steps or [] if isinstance(s, dict) and "run" in s)


def gate_problems(root: Path) -> list[str]:
    """ci.yml and release.yml must both run every quality gate, and the release's build must wait for the job that runs them."""
    try:
        import yaml
    except ImportError:
        return []  # yaml_problems already reports the missing PyYAML
    found: list[str] = []
    for name in ("ci.yml", "release.yml"):
        path = root / ".github" / "workflows" / name
        if not path.exists():
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue  # reported by yaml_problems
        jobs = doc.get("jobs") if isinstance(doc, dict) else None
        jobs = jobs if isinstance(jobs, dict) else {}
        text = "\n".join(_run_text(j) for j in jobs.values())
        found += [f"{name}: no step runs the gate '{g}'" for g in GATES if g not in text]
        if name == "release.yml":
            validators = [jid for jid, j in jobs.items() if all(g in _run_text(j) for g in GATES)]
            needs = jobs.get("build", {}).get("needs", []) if isinstance(jobs.get("build"), dict) else []
            needs = [needs] if isinstance(needs, str) else list(needs)
            if not validators:
                found.append("release.yml: no single job runs every gate before the build")
            elif not set(validators) & set(needs):
                found.append(f"release.yml: the build job does not need the validation job ({', '.join(validators)})")
    return found


def problems(tag: str, root: Path = ROOT, dist: Path | None = None) -> list[str]:
    version = tag[1:] if tag.startswith("v") else tag
    found: list[str] = []
    if not SEMVER.match(version):
        found.append(f"tag {tag!r} is not a version like v1.2.3")
    py = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"))
    init = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"', (root / "db_lens" / "__init__.py").read_text(encoding="utf-8"))
    log = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not py or py[1] != version:
        found.append(f"pyproject.toml version is {py[1] if py else 'missing'}, tag says {version}")
    if not init or init[1] != version:
        found.append(f"db_lens/__init__.py __version__ is {init[1] if init else 'missing'}, tag says {version}")
    if not re.search(rf"(?m)^## {re.escape(version)}\b", log):
        found.append(f"CHANGELOG.md has no '## {version}' section")
    found += unpinned_actions(root) + tool_drift(root) + stale_versions(root, version) + doc_drift(root) + yaml_problems(root)
    found += skip_problems(root) + pragma_problems(root) + gate_problems(root)
    if dist is not None:
        files = sorted(p.name for p in dist.glob("*") if p.suffix in (".whl", ".gz"))
        if len(files) != 2:
            found.append(f"expected one wheel and one sdist in {dist}, found {files}")
        for name in files:
            if not re.match(rf"db[_-]lens-{re.escape(version)}(-|\.tar\.gz)", name):
                found.append(f"artifact {name} does not carry version {version}")
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tag", nargs="?", help="git tag, e.g. v0.7.1 (default: v<version in pyproject.toml>)")
    ap.add_argument("--dist", type=Path, help="directory with the built wheel and sdist")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.tag is None:
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (a.root / "pyproject.toml").read_text(encoding="utf-8"))
        a.tag = f"v{m[1] if m else '?'}"
    found = problems(a.tag, a.root, a.dist)
    for line in found:
        print(f"release check FAILED: {line}")
    if not found:
        print(f"release check OK: {a.tag} matches pyproject.toml, __version__, CHANGELOG" + (" and the built artifacts" if a.dist else ""))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
