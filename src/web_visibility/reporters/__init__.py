"""Report renderers: terminal (Rich), JSON (machine-readable) and Markdown."""

from web_visibility.reporters.json_report import build_report, to_json, write_json
from web_visibility.reporters.markdown_report import render_markdown, write_markdown
from web_visibility.reporters.terminal import render_terminal

__all__ = [
    "build_report",
    "render_markdown",
    "render_terminal",
    "to_json",
    "write_json",
    "write_markdown",
]
