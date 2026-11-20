"""Stage 12: release metadata, CI workflow, version consistency, and style invariants."""

import json
import re
from pathlib import Path

import yaml

import amplisentinel

ROOT = Path(__file__).resolve().parents[1]


def _read_pyproject_meta() -> tuple[str, str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib
    except ModuleNotFoundError:
        name = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)
        version = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)
        return name, version
    pyproject = tomllib.loads(text)
    return pyproject["project"]["name"], pyproject["project"]["version"]


def test_version_matches_pyproject_citation_and_changelog():
    name, version = _read_pyproject_meta()
    assert name == "amplisentinel"
    assert amplisentinel.__version__ == version

    cff = yaml.safe_load((ROOT / "CITATION.cff").read_text(encoding="utf-8"))
    assert cff["cff-version"] == "1.2.0"
    assert cff["version"] == version
    assert cff["license"] == "BSD-3-Clause"

    zenodo = json.loads((ROOT / ".zenodo.json").read_text(encoding="utf-8"))
    assert zenodo["license"] == "BSD-3-Clause"

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"[{version}]" in changelog


def test_ci_workflow_runs_pytest_and_ruff_on_linux_and_macos():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job = wf["jobs"]["test-and-lint"]
    oses = job["strategy"]["matrix"]["os"]
    assert "ubuntu-latest" in oses and "macos-latest" in oses
    commands = "\n".join(s.get("run", "") for s in job["steps"])
    assert "pytest" in commands and "ruff check" in commands


def test_no_em_or_en_dashes_in_code_or_docs():
    paths = [
        *ROOT.joinpath("amplisentinel").rglob("*.py"),
        *ROOT.joinpath("tests").rglob("*.py"),
        *ROOT.joinpath("docs").rglob("*"),
        ROOT / "README.md",
        ROOT / "CLAUDE.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CITATION.cff",
        ROOT / "environment.yml",
    ]
    for p in paths:
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            assert "\u2014" not in text, f"em dash found in {p}"
            assert "\u2013" not in text, f"en dash found in {p}"
