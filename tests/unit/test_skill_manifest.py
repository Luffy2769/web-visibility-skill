"""Validate the Agent Skill against the Agent Skills specification (agentskills.io).

Agents parse SKILL.md frontmatter with varying strictness, so the manifest is
validated in CI rather than assuming every agent tolerates mistakes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

SKILL_DIR = Path(__file__).parents[2] / ".agents" / "skills" / "web-visibility"
SKILL_FILE = SKILL_DIR / "SKILL.md"

ALLOWED_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_SKILL_LINES = 500


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter delimited by ---"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict), "frontmatter must be a YAML mapping"
    return data, match.group(2)


@pytest.fixture(scope="module")
def manifest() -> tuple[dict[str, Any], str]:
    return split_frontmatter(SKILL_FILE.read_text(encoding="utf-8"))


def test_only_specified_fields(manifest: tuple[dict[str, Any], str]) -> None:
    fields, _ = manifest
    assert set(fields) <= ALLOWED_FIELDS, f"unsupported fields: {set(fields) - ALLOWED_FIELDS}"


def test_name(manifest: tuple[dict[str, Any], str]) -> None:
    name = manifest[0]["name"]
    assert isinstance(name, str) and 1 <= len(name) <= 64
    assert NAME_PATTERN.match(name), "lowercase letters, digits and single hyphens only"
    assert name == SKILL_DIR.name, "name must match the skill directory name"


def test_description(manifest: tuple[dict[str, Any], str]) -> None:
    description = manifest[0]["description"]
    assert isinstance(description, str) and description.strip()
    assert len(description) <= 1024
    assert "Use when" in description, "description should say when to use the skill"


def test_optional_fields(manifest: tuple[dict[str, Any], str]) -> None:
    fields = manifest[0]
    if "compatibility" in fields:
        assert isinstance(fields["compatibility"], str) and len(fields["compatibility"]) <= 500
    if "metadata" in fields:
        assert all(
            isinstance(k, str) and isinstance(v, str) for k, v in fields["metadata"].items()
        ), "metadata must map strings to strings"


def test_body_is_concise(manifest: tuple[dict[str, Any], str]) -> None:
    lines = SKILL_FILE.read_text(encoding="utf-8").count("\n")
    assert lines < MAX_SKILL_LINES, f"SKILL.md has {lines} lines; move detail to references/"


def test_relative_links_resolve(manifest: tuple[dict[str, Any], str]) -> None:
    _, body = manifest
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", body):
        if "://" in target:
            continue
        assert (SKILL_DIR / target).exists(), f"broken link in SKILL.md: {target}"


def test_metadata_version_matches_package(manifest: tuple[dict[str, Any], str]) -> None:
    from web_visibility import __version__

    assert manifest[0]["metadata"]["version"] == __version__


def test_declared_cli_and_schema_compatibility_match_the_package(
    manifest: tuple[dict[str, Any], str],
) -> None:
    from web_visibility import REPORT_SCHEMA_VERSION, __version__

    metadata = manifest[0]["metadata"]
    low, high = (part.strip().lstrip(">=<") for part in metadata["requires-cli"].split(","))
    version = tuple(int(p) for p in __version__.split("."))
    assert tuple(int(p) for p in low.split(".")) <= version < tuple(int(p) for p in high.split("."))
    assert metadata["report-schema"] == REPORT_SCHEMA_VERSION.split(".")[0]
    assert f"@v{__version__}" in manifest[0]["compatibility"]  # install command pins this release


def test_every_documented_install_ref_pins_this_release() -> None:
    """Audit #2 N7: SKILL.md pointed at a tag that did not exist. Every pinned ref in
    the docs must be this version's tag (the release workflow then checks the tag)."""
    from web_visibility import __version__

    root = Path(__file__).parents[2]
    docs = [
        root / "README.md",
        root / "SECURITY.md",
        root / ".agents/skills/web-visibility/SKILL.md",
    ]
    refs = {ref for doc in docs for ref in re.findall(r"@v(\d+\.\d+\.\d+)", doc.read_text("utf-8"))}
    assert refs == {__version__}
