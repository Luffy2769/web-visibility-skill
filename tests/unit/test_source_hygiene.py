"""Guard against invisible / bidirectional control characters in the repository.

Such characters ("Trojan Source", CVE-2021-42574) can make code or skill
instructions read differently to humans and to machines. Escape them instead
(e.g. ``\\u202e`` inside a regex).
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCANNED = ("src", "tests", ".agents", "docs", "evals")
SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".txt", ".html", ".xml", ".json"}


def test_no_invisible_format_characters() -> None:
    offenders = []
    for folder in SCANNED:
        for path in (ROOT / folder).rglob("*"):
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                bad = {f"U+{ord(c):04X}" for c in line if unicodedata.category(c) == "Cf"}
                if bad - {"U+FEFF"} or ("U+FEFF" in bad and lineno > 1):
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno} {sorted(bad)}")
    assert not offenders, "invisible format characters found:\n" + "\n".join(offenders)
