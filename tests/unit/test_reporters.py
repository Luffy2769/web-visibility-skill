import io
import json

from rich.console import Console

from web_visibility.audit import AuditReport
from web_visibility.reporters import build_report, render_markdown, render_terminal, to_json
from web_visibility.reporters._text import clean, md, md_code

from factories import good_head, html_doc, make_page
from factories import make_report as _make_report

HOSTILE_TITLE = "[bold red]pwned[/] [2J<script>alert(1)</script> *x* | y"


def make_report(*pages) -> AuditReport:  # type: ignore[no-untyped-def]
    return _make_report(pages)


def sample_report() -> AuditReport:
    return make_report(
        make_page(
            "/", html_doc(good_head("/"), '<h1>Home</h1><a href="/a">A</a><img src="x.png">')
        ),
        make_page("/a", html_doc(f"<title>{HOSTILE_TITLE}</title>", "<h1>A</h1><h1>B</h1>")),
        make_page("/b", html_doc(f"<title>{HOSTILE_TITLE}</title>", '<h1>B</h1><a href="/">H</a>')),
    )


def test_json_report_has_stable_top_level_structure() -> None:
    data = build_report(sample_report())
    assert list(data) == [
        "schema_version",
        "project",
        "version",
        "generated_at",
        "target",
        "crawl",
        "score",
        "summary",
        "issues",
        "pages",
        "robots",
        "sitemap",
        "structured_data",
        "limitations",
    ]
    assert data["project"] == "web-visibility"
    assert data["score"]["name"] == "Web Visibility Diagnostic Score"
    assert "not a ranking" in data["score"]["disclaimer"]
    assert set(data["score"]["categories"]) == {
        "technical",
        "metadata",
        "structure",
        "links",
        "images",
        "structured_data",
    }


def test_json_issue_and_page_fields() -> None:
    data = build_report(sample_report())
    issue = data["issues"][0]
    for key in (
        "id",
        "category",
        "severity",
        "title",
        "description",
        "evidence",
        "affected_url",
        "affected_urls",
        "recommendation",
        "confidence",
        "details",
    ):
        assert key in issue
    page = data["pages"][0]
    for key in (
        "url",
        "status_code",
        "title",
        "canonical",
        "h1s",
        "headings",
        "links",
        "images",
        "structured_data",
        "issue_ids",
        "response_headers",
    ):
        assert key in page
    assert data["summary"]["issues_total"] == len(data["issues"])


def test_json_round_trips_hostile_content() -> None:
    """Site text reaches JSON unchanged, as data. Compared with what the parser
    extracted rather than a literal: html.parser keeps ``<script>`` inside
    ``<title>`` as text on Python 3.13+ (as browsers do) but parses it as a tag
    on 3.11/3.12."""
    report = sample_report()
    extracted = {p.title for p in report.crawl.pages if p.title and "pwned" in p.title}
    assert extracted
    titles = {p.get("title") for p in json.loads(to_json(report))["pages"]}
    assert extracted <= titles
    title = next(iter(extracted))
    assert "[bold red]pwned[/]" in title and "\x1b[2J" in title


def test_markdown_sections_and_escaping() -> None:
    text = render_markdown(sample_report())
    for heading in (
        "## Executive Summary",
        "## Score",
        "## Crawl Summary",
        "### Category Scores",
        "## Issues",
        "## Recommendations",
        "## Technical Findings",
        "## Affected Pages",
        "## Limitations",
    ):
        assert heading in text
    assert "<script>" not in text
    assert "\x1b" not in text
    assert "Web Visibility Diagnostic Score" in text


def test_terminal_output_does_not_interpret_site_markup() -> None:
    buffer = io.StringIO()
    console = Console(file=buffer, width=120, force_terminal=False, color_system=None)
    render_terminal(sample_report(), console, show_info=True)
    output = buffer.getvalue()
    assert "WEB VISIBILITY AUDIT" in output
    assert "\x1b" not in output
    assert "Duplicate page titles" in output
    assert "[bold red]pwned[/]" in output  # shown literally, not rendered as markup


def test_unscored_report_renders() -> None:
    report = make_report(make_page("/", error="connection refused", error_kind="connection"))
    assert not report.auditable
    assert build_report(report)["score"]["overall"] is None
    assert "no score was computed" in render_markdown(report)
    buffer = io.StringIO()
    render_terminal(report, Console(file=buffer, width=100))
    assert "not scored" in buffer.getvalue()


def test_text_helpers() -> None:
    assert clean("a\nb\x1b[31mc\u202e") == "a b[31mc"
    assert clean("abcdef", limit=5) == "ab..."
    assert md("<b>|x|</b>") == "&lt;b&gt;\\|x\\|&lt;/b&gt;"
    assert md_code("a`b") == "`a'b`"
