import re

from typer.testing import CliRunner

from web_visibility import __version__
from web_visibility.cli import _directory_name, app

runner = CliRunner()
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """Output without ANSI styling. Rich styles help text when GITHUB_ACTIONS is set,
    which splits ``--max-pages`` into separately styled pieces (the first CI run failed
    on exactly this)."""
    return _ANSI.sub("", text)


def test_help_lists_audit_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "audit" in plain(result.output)


def test_audit_help_lists_options() -> None:
    result = runner.invoke(
        app, ["audit", "--help"], env={"COLUMNS": "200", "TERMINAL_WIDTH": "200"}
    )
    assert result.exit_code == 0
    for option in ("--max-pages", "--timeout", "--output", "--format", "--allow-private-network"):
        assert option in plain(result.output)


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in plain(result.output)


def test_invalid_url_exits_with_usage_error() -> None:
    result = runner.invoke(app, ["audit", "example.com", "--quiet"])
    assert result.exit_code == 2
    assert "not an absolute http(s) URL" in plain(result.output)


def test_localhost_is_blocked_without_opt_in() -> None:
    result = runner.invoke(app, ["audit", "http://127.0.0.1:9/", "--quiet", "--delay", "0"])
    assert result.exit_code == 1
    assert "--allow-private-network" in plain(result.output)


def test_max_pages_validation() -> None:
    result = runner.invoke(app, ["audit", "https://example.com", "--max-pages", "0"])
    assert result.exit_code == 2


def test_directory_name_is_filesystem_safe() -> None:
    assert _directory_name("https://example.com/") == "example.com"
    assert _directory_name("http://127.0.0.1:8000/") == "127.0.0.1_8000"
