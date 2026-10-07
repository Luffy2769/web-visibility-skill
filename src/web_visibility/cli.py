"""Command-line interface: ``web-visibility audit <URL>``.

Output streams are kept separate so the CLI composes well with other tools:

* stdout - the report in the selected ``--format`` (terminal, json or markdown)
* stderr - progress, warnings and errors

Exit codes: 0 = audit completed; 1 = the start URL could not be audited
(reports are still written); 2 = invalid usage or invalid URL; 3 = internal error
(no report; any previous report in the output directory is removed first, so a
stale file can never be mistaken for this run's result); 130 = interrupted.
"""

from __future__ import annotations

import logging
import re
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.markup import escape

from web_visibility import REPORT_SCHEMA_VERSION, __version__
from web_visibility.audit import AuditReport, run_audit
from web_visibility.config import CrawlConfig, FetchConfig
from web_visibility.crawler import InvalidStartURLError
from web_visibility.reporters import (
    render_markdown,
    render_terminal,
    to_json,
    write_json,
    write_markdown,
)
from web_visibility.urls import normalize_url

app = typer.Typer(
    name="web-visibility",
    help="Deterministic website visibility auditor (Phase 01: technical SEO foundation).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


class OutputFormat(StrEnum):
    TERMINAL = "terminal"
    JSON = "json"
    MARKDOWN = "markdown"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"web-visibility {__version__} (report schema {REPORT_SCHEMA_VERSION})")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Audit websites for technical visibility issues using deterministic checks."""


@app.command()
def audit(
    url: Annotated[
        str, typer.Argument(help="Absolute http(s) URL to audit, e.g. https://example.com")
    ],
    max_pages: Annotated[
        int, typer.Option("--max-pages", "-n", min=1, max=1000, help="Maximum pages to crawl.")
    ] = 20,
    timeout: Annotated[
        float, typer.Option(min=1.0, max=120.0, help="Per-request timeout in seconds.")
    ] = 10.0,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            file_okay=False,
            help="Directory for audit.json and audit.md (written to <dir>/<host>/).",
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="What to print to stdout.")
    ] = OutputFormat.TERMINAL,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress the terminal report and progress.")
    ] = False,
    delay: Annotated[
        float, typer.Option(min=0.0, max=10.0, help="Minimum seconds between requests.")
    ] = 0.25,
    max_link_checks: Annotated[
        int,
        typer.Option(min=0, max=1000, help="Maximum uncrawled internal link targets to verify."),
    ] = 50,
    max_page_bytes: Annotated[
        int,
        typer.Option(min=100_000, max=100_000_000, help="Maximum decompressed page size in bytes."),
    ] = 5_000_000,
    check_external: Annotated[
        bool, typer.Option(help="Also verify external link targets (up to 25).")
    ] = False,
    ignore_robots: Annotated[
        bool,
        typer.Option(help="Crawl URLs even if robots.txt disallows them (your own sites only)."),
    ] = False,
    allow_private_network: Annotated[
        bool, typer.Option(help="Allow localhost/private-network targets (local development).")
    ] = False,
    show_info: Annotated[
        bool, typer.Option(help="Include informational notes in the terminal report.")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Log every request to stderr.")
    ] = False,
) -> None:
    """Crawl a website and report evidence-backed technical findings."""
    err = Console(stderr=True)
    _configure_logging(verbose)
    directory = output / _directory_name(normalize_url(url) or url) if output is not None else None
    if directory is not None:
        _remove_previous_reports(directory)
    try:
        config = CrawlConfig(
            max_pages=max_pages,
            max_link_checks=max_link_checks,
            check_external=check_external,
            respect_robots=not ignore_robots,
            fetch=FetchConfig(
                timeout=timeout,
                delay=delay,
                allow_private=allow_private_network,
                max_bytes=max_page_bytes,
            ),
        )
        report = _run(url, config, err, show_progress=not quiet and err.is_terminal)
    except InvalidStartURLError as exc:
        err.print(f"[bold red]Error:[/] {escape(str(exc))}", highlight=False)
        raise typer.Exit(2) from exc
    except KeyboardInterrupt:
        err.print("[yellow]Interrupted.[/]")
        raise typer.Exit(130) from None
    except Exception as exc:  # last resort: never die with a bare traceback and no verdict
        logging.getLogger(__name__).debug("internal error", exc_info=True)
        err.print(
            f"[bold red]Internal error:[/] {escape(type(exc).__name__)}: {escape(str(exc))}. "
            "No report was written. Re-run with --verbose and please report this as a bug.",
            highlight=False,
        )
        raise typer.Exit(3) from exc

    written: list[Path] = []
    if directory is not None:
        written = [
            write_json(report, directory / "audit.json"),
            write_markdown(report, directory / "audit.md"),
        ]

    if output_format is OutputFormat.JSON:
        _write_stdout(to_json(report) + "\n")
    elif output_format is OutputFormat.MARKDOWN:
        _write_stdout(render_markdown(report))
    elif not quiet:
        render_terminal(report, Console(), show_info=show_info, written=written)
    if written and (quiet or output_format is not OutputFormat.TERMINAL):
        for path in written:
            err.print(f"Wrote {path}", highlight=False, markup=False)

    if report.auditable and report.score.overall is None:
        err.print(
            "[bold yellow]Partial audit:[/] no score was computed - "
            f"{escape(report.score.reason or report.score.status)}.",
        )
    if not report.auditable:
        reasons = escape("; ".join(report.crawl.state_reasons))
        err.print(
            f"[bold red]Audit incomplete:[/] {reasons}. No score was computed; see the "
            "'start-url-unavailable' finding for details.",
        )
        if not allow_private_network and _blocked(report):
            err.print("Hint: local or private-network targets need --allow-private-network.")
        raise typer.Exit(1)


def _run(url: str, config: CrawlConfig, err: Console, *, show_progress: bool) -> AuditReport:
    if not show_progress:
        return run_audit(url, config)
    with err.status("Starting audit...") as status:
        return run_audit(
            url, config, progress=lambda message: status.update(_progress_text(message))
        )


def _progress_text(message: str) -> str:
    # Progress messages contain URLs from the audited site; never treat them as markup.
    return escape(message[:150])


def _blocked(report: AuditReport) -> bool:
    """True if the SSRF guard refused the robots.txt or start URL request."""
    start = report.crawl.pages[0] if report.crawl.pages else None
    return report.crawl.robots.error_kind == "blocked" or (
        start is not None and start.error_kind == "blocked"
    )


def _remove_previous_reports(directory: Path) -> None:
    for name in ("audit.json", "audit.md"):
        (directory / name).unlink(missing_ok=True)


def _directory_name(start_url: str) -> str:
    netloc = urlsplit(start_url).netloc or "site"
    return re.sub(r"[^A-Za-z0-9._-]", "_", netloc)


def _write_stdout(text: str) -> None:
    """Write UTF-8 regardless of the console's code page (Windows pipes)."""
    sys.stdout.flush()
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
