"""Regression tests for independent audit #2, findings N1 and N2.

N1: malformed, site-controlled input crashed the whole audit (no report) or made
    a whole page "unparseable".
N2: pages and link targets the auditor could not observe were counted as checked,
    so an audit that saw 1 page in 20 reported itself complete with a high score.

Everything runs through the real crawler over a real local socket.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from web_visibility.models import Severity

from live_server import LiveServer, html_page, live_audit, live_server, send, send_raw

pytestmark = pytest.mark.integration

BAD_URLS = ["http://[bad", "http://[::1", "//[x]/", "http://[not-an-ip]/"]


def _scores(report) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {c.category.value: c for c in report.score.categories}


# =========================================================================== #
# N1 - malformed input must never crash the audit
# =========================================================================== #


class TestN1MalformedInputIsRecoverable:
    @pytest.mark.parametrize("href", BAD_URLS)
    @pytest.mark.parametrize("markup", ['<a href="{u}">x</a>', '<img src="{u}" alt="x">'])
    def test_malformed_link_or_image_url_does_not_lose_the_page(
        self, markup: str, href: str
    ) -> None:
        with live_server() as srv:
            srv.page("/", html_page("Home page title", links=("/ok",), body=markup.format(u=href)))
            srv.page("/ok", html_page("Second page title"))
            report = live_audit(srv)
        start = report.context.start_page
        assert start is not None and start.content is not None, start and start.error
        assert report.crawl.state == "complete"
        assert report.score.overall is not None

    @pytest.mark.parametrize("href", BAD_URLS)
    def test_malformed_base_href_falls_back_to_the_page_url(self, href: str) -> None:
        with live_server() as srv:
            srv.page(
                "/", html_page("Home page title", links=("/ok",), head=f'<base href="{href}">')
            )
            srv.page("/ok", html_page("Second page title"))
            report = live_audit(srv)
        assert report.pages_audited == 2

    @pytest.mark.parametrize("href", BAD_URLS)
    def test_malformed_html_canonical_is_reported_not_raised(self, href: str) -> None:
        with live_server() as srv:
            srv.page(
                "/", html_page("Home page title", head=f'<link rel="canonical" href="{href}">')
            )
            report = live_audit(srv)
        assert "malformed-canonical" in {i.id for i in report.issues}
        assert report.score.overall is not None

    def test_malformed_link_header_canonical_is_reported_not_raised(self) -> None:
        with live_server() as srv:
            srv.page("/", html_page("Home page title"),
                     headers={"Link": '<http://[bad>; rel="canonical"'})  # fmt: skip
            report = live_audit(srv)
        assert "malformed-canonical" in {i.id for i in report.issues}

    def test_malformed_redirect_on_the_start_url_is_a_failed_fetch(self) -> None:
        with live_server() as srv:
            srv.page("/", b"", status=301, headers={"Location": "http://[bad"})
            report = live_audit(srv)
        assert report.crawl.state == "failed"
        start = report.context.start_page
        assert start is not None and start.error_kind == "invalid-redirect"

    def test_malformed_redirect_on_a_linked_page_does_not_stop_the_audit(self) -> None:
        with live_server() as srv:
            srv.page("/", html_page("Home page title", links=("/moved",)))
            srv.page("/moved", b"", status=302, headers={"Location": "http://[bad"})
            report = live_audit(srv)
        assert report.context.start_page is not None
        assert report.context.start_page.content is not None
        moved = report.context.page_index[srv.base + "/moved"]
        assert moved.error_kind == "invalid-redirect"

    @pytest.mark.parametrize("charset", ["bogus", "hex", "base64", '""', "rot13"])
    def test_unusable_robots_charset_falls_back_to_utf8(self, charset: str) -> None:
        with live_server() as srv:
            srv.page("/robots.txt", "User-agent: *\nDisallow: /private\n",
                     content_type=f"text/plain; charset={charset}")  # fmt: skip
            srv.page("/", html_page("Home page title"))
            report = live_audit(srv)
        assert report.crawl.robots.state == "parsed"
        assert report.crawl.robots.rules is not None
        assert not report.crawl.robots.allows(srv.base + "/private/x", "WebVisibilitySkill")

    def test_unusable_page_charset_is_ignored(self) -> None:
        with live_server() as srv:
            srv.page("/", html_page("Home page title"), content_type="text/html; charset=bogus")
            report = live_audit(srv)
        assert report.pages_audited == 1

    def test_cli_never_exits_with_a_traceback_and_never_leaves_a_stale_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unexpected internal error: exit 3, a clear message, no stale audit.json."""
        from typer.testing import CliRunner

        import web_visibility.cli as cli

        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated internal failure")

        stale = tmp_path / "example.com"
        stale.mkdir()
        (stale / "audit.json").write_text('{"stale": true}', encoding="utf-8")
        (stale / "audit.md").write_text("stale", encoding="utf-8")
        monkeypatch.setattr(cli, "run_audit", explode)
        result = CliRunner().invoke(cli.app, ["audit", "https://example.com/", "-o", str(tmp_path)])
        assert result.exit_code == 3
        assert "Internal error" in result.output
        assert not (stale / "audit.json").exists()
        assert not (stale / "audit.md").exists()

    def test_cli_produces_a_report_for_a_hostile_page(self, tmp_path: Path) -> None:
        """The exact audit #2 reproduction, through the installed entry point."""
        with live_server() as srv:
            srv.page(
                "/", html_page("Canon page title", head='<link rel="canonical" href="http://[bad">')
            )
            proc = subprocess.run(
                [sys.executable, "-m", "web_visibility", "audit", srv.base + "/",
                 "--allow-private-network", "--delay", "0", "--quiet", "-o", str(tmp_path)],
                capture_output=True, text=True, timeout=120,
            )  # fmt: skip
        assert proc.returncode == 0, proc.stderr
        assert "Traceback" not in proc.stderr
        data = json.loads(next(tmp_path.glob("*/audit.json")).read_text(encoding="utf-8"))
        assert "malformed-canonical" in {i["id"] for i in data["issues"]}


# =========================================================================== #
# N2 - what was not observed is never reported as checked
# =========================================================================== #


def _site_with_19_failing_pages(srv: LiveServer, respond) -> None:  # type: ignore[no-untyped-def]
    links = tuple(f"/p{i}" for i in range(19))
    srv.page("/", html_page("Home page title", links=links))
    for path in links:
        srv.route(path, respond)


def _drop_mid_body(h) -> None:  # type: ignore[no-untyped-def]
    send_raw(h, b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 5000\r\n\r\n<html>")


class TestN2CoverageIsNeverOverstated:
    def test_bot_protection_after_the_first_page_is_a_partial_audit_without_a_score(self) -> None:
        """Start page 200, every other page 403: was 'complete', 92/100, links 20/20."""
        with live_server() as srv:
            _site_with_19_failing_pages(srv, lambda h: send(h, 403, b"<title>Denied</title>"))
            report = live_audit(srv)
        assert report.crawl.state == "partial"
        assert any("19 of 20 crawled page(s) could not be retrieved" in r
                   for r in report.crawl.state_reasons)  # fmt: skip
        categories = _scores(report)
        for name in ("technical", "metadata", "structure", "structured_data", "links"):
            assert categories[name].points is None, name  # type: ignore[attr-defined]
        assert report.score.overall is None
        assert report.score.status == "insufficient-coverage"
        denied = [i for i in report.issues if i.id == "pages-access-denied"]
        assert len(denied) == 1 and len(denied[0].details["statuses"]) == 19

    @pytest.mark.parametrize(
        "respond",
        [
            pytest.param(_drop_mid_body, id="connection-dropped"),
            pytest.param(
                lambda h: send(h, 200, b"\x1b\x00", headers={"Content-Encoding": "br"}),
                id="unsupported-encoding",
            ),
        ],
    )
    def test_most_pages_unretrievable_is_never_complete(self, respond) -> None:  # type: ignore[no-untyped-def]
        with live_server() as srv:
            _site_with_19_failing_pages(srv, respond)
            report = live_audit(srv)
        assert report.crawl.state == "partial"
        assert report.score.status in ("insufficient-coverage", "not-scored")
        assert report.score.overall is None
        assert _scores(report)["links"].coverage.status == "not-scored"  # type: ignore[attr-defined]

    def test_unverifiable_link_targets_are_not_scored_as_passing(self) -> None:
        """30 links answering 403 were 'scored 30/30' with full marks."""
        with live_server() as srv:
            links = tuple(f"/l{i}" for i in range(30))
            srv.page("/", html_page("Home page title", links=links))
            srv.default = lambda h: send(h, 403, b"forbidden")
            report = live_audit(srv, max_pages=1)
        links_score = _scores(report)["links"]
        assert links_score.points is None  # type: ignore[attr-defined]
        assert links_score.coverage.status == "not-scored"  # type: ignore[attr-defined]
        assert links_score.coverage.checked == 0  # type: ignore[attr-defined]
        assert "definitive" in (links_score.coverage.reason or "")  # type: ignore[attr-defined]

    def test_a_minority_of_failures_keeps_the_score_but_marks_it_partial(self) -> None:
        with live_server() as srv:
            links = tuple(f"/p{i}" for i in range(5))
            srv.page("/", html_page("Home page title", links=links))
            for path in links[:4]:
                srv.page(path, html_page(f"Page {path} title"))
            srv.route(links[4], lambda h: send(h, 403, b"denied"))
            report = live_audit(srv)
        assert report.crawl.state == "partial"
        assert report.score.overall is not None
        assert report.score.status == "partial"
        assert _scores(report)["metadata"].coverage.status == "partial"  # type: ignore[attr-defined]

    def test_real_http_errors_are_observations_not_coverage_gaps(self) -> None:
        """19 of 20 pages 404: the site is broken, and the audit saw that completely."""
        with live_server() as srv:
            _site_with_19_failing_pages(srv, lambda h: send(h, 404, b"<title>404</title>"))
            report = live_audit(srv)
        assert report.crawl.state == "complete"
        assert report.score.overall is not None and report.score.overall < 65
        error = next(i for i in report.issues if i.id == "crawled-page-error")
        assert error.severity is Severity.CRITICAL

    def test_oversized_link_target_still_counts_as_a_working_link(self) -> None:
        with live_server() as srv:
            srv.page("/", html_page("Home page title", links=("/big", "/ok")))
            srv.page("/big", "<html><body>" + "x" * 200_000 + "</body></html>")
            srv.page("/ok", html_page("Second page title"))
            report = live_audit(srv, fetch={"max_bytes": 100_000})
        assert "broken-internal-link" not in {i.id for i in report.issues}
        assert "unverified-link" not in {i.id for i in report.issues}
        assert _scores(report)["links"].coverage.checked == 2  # type: ignore[attr-defined]

    def test_json_report_exposes_unretrieved_pages_and_the_score_reason(self) -> None:
        from web_visibility.reporters.json_report import build_report

        with live_server() as srv:
            _site_with_19_failing_pages(srv, lambda h: send(h, 403, b"denied"))
            data = build_report(live_audit(srv))
        assert data["crawl"]["pages_unretrieved"] == 19
        assert data["crawl"]["unretrieved"][0]["reason"] == "HTTP 403"
        assert data["score"]["overall"] is None
        assert data["score"]["status"] == "insufficient-coverage"
        assert "could be measured" in data["score"]["reason"]

    def test_markdown_and_terminal_say_partial_and_show_no_number(self) -> None:
        import io

        from rich.console import Console

        from web_visibility.reporters import render_markdown, render_terminal

        with live_server() as srv:
            _site_with_19_failing_pages(srv, lambda h: send(h, 403, b"denied"))
            report = live_audit(srv)
        markdown = render_markdown(report)
        assert "PARTIAL AUDIT" in markdown
        assert not re.search(r"Score: \*?\*?\d+/100", markdown)  # no headline number
        buffer = io.StringIO()
        render_terminal(report, Console(file=buffer, width=160))
        assert "not scored" in buffer.getvalue() and "PARTIAL" in buffer.getvalue()
