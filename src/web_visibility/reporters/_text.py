"""Sanitizing untrusted text for human-facing reports.

Titles, anchors, URLs and evidence strings come from the audited website,
which may be hostile. Before display they must not be able to:

* inject terminal escape sequences (cursor movement, colour, hyperlinks),
* inject Rich console markup (``[link=...]``),
* inject Markdown/HTML into rendered reports.

JSON output needs none of this: ``json.dumps`` escapes everything.
"""

from __future__ import annotations

import re

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_NEWLINES = re.compile(r"[\r\n\t]+")
# Characters that can form Markdown syntax mid-line: emphasis, code, links/images,
# table cells and strikethrough. "<" and ">" are entity-encoded separately.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]|~])")


def clean(value: object, limit: int | None = None) -> str:
    """Strip control/bidi characters, flatten newlines and optionally truncate."""
    text = _NEWLINES.sub(" ", str(value))
    text = _CONTROL_CHARS.sub("", text)
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 3)] + "..."
    return text


def md(value: object, limit: int | None = None) -> str:
    """Escape text for safe inclusion in Markdown (including table cells)."""
    text = clean(value, limit)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def md_code(value: object, limit: int | None = None) -> str:
    """Render text as inline code, safe even if it contains backticks."""
    text = clean(value, limit).replace("`", "'")
    return f"`{text}`" if text else "``"
