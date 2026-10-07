"""Static validation of the GitHub Actions workflow.

This cannot prove the workflow passes on GitHub; it guards against syntax
errors, unpinned actions and a Python matrix that drifts from pyproject.toml.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_workflow_is_valid_yaml_with_expected_jobs() -> None:
    data = _workflow()
    assert set(data["jobs"]) == {"lint", "test", "mutation", "package"}
    assert data["permissions"] == {"contents": "read"}
    for job in data["jobs"].values():
        assert job["runs-on"] and job["steps"]


def test_third_party_actions_are_pinned_to_commit_shas() -> None:
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOW.read_text(encoding="utf-8"))
    assert uses
    for ref in uses:
        _, _, version = ref.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", version), f"{ref} is not pinned to a commit SHA"


def test_python_matrix_matches_declared_support() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    classifiers = {
        c.rsplit(" :: ", 1)[1]
        for c in project["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)
    }
    matrix = set(_workflow()["jobs"]["test"]["strategy"]["matrix"]["python-version"])
    assert matrix == classifiers
    minimum = project["requires-python"].removeprefix(">=")
    assert min(matrix, key=lambda v: tuple(map(int, v.split(".")))) == minimum
