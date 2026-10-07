"""End-to-end audit of the local fixture website (tests/fixtures/site).

The fixture contains known, intentional problems. These tests assert that the
audit finds each of them on the right page, and - just as important - that it
does NOT report problems that are not there (false positives).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from web_visibility.audit import AuditReport, run_audit
from web_visibility.cli import app
from web_visibility.crawler import CrawlConfig
from web_visibility.fetch import FetchConfig
from web_visibility.models import Issue

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def report(fixture_site: str) -> AuditReport:
    config = CrawlConfig(max_pages=20, fetch=FetchConfig(delay=0, allow_private=True))
    return run_audit(f"{fixture_site}/", config)


def paths(urls: tuple[str, ...] | list[str], base: str) -> set[str]:
    return {u.removeprefix(base) for u in urls}


def find(report: AuditReport, rule_id: str) -> list[Issue]:
    return [i for i in report.issues if i.id == rule_id]


EXPECTED = {
    # rule id                     -> pages it must be reported on (paths)
    "missing-title": {"/services.html"},
    "duplicate-title": {"/about.html", "/duplicate.html"},
    "missing-meta-description": {"/about.html"},
    "duplicate-meta-description": {"/services.html", "/duplicate.html"},
    "missing-canonical": {"/services.html"},
    "multiple-h1": {"/about.html"},
    "heading-level-skip": {"/services.html"},
    "empty-heading": {"/services.html"},
    "broken-internal-link": {"/"},  # / links to /contact.html (404)
    "internal-link-redirect": {"/"},  # / links to /old-page (301)
    "image-missing-alt": {"/"},
    "invalid-json-ld": {"/services.html"},
    "potential-orphan-page": {"/orphan.html"},
    "sitemap-url-error": {"/gone.html"},
    "sitemap-url-redirects": {"/old-page"},
    "sitemap-url-noindex": {"/noindex.html"},
    "empty-anchor-text": {"/services.html"},
}


@pytest.mark.parametrize(("rule_id", "expected_paths"), sorted(EXPECTED.items()))
def test_known_problem_is_detected_on_the_right_pages(
    report: AuditReport, fixture_site: str, rule_id: str, expected_paths: set[str]
) -> None:
    issues = find(report, rule_id)
    assert issues, f"{rule_id} was not reported"
    found = set().union(*(paths(i.affected_urls, fixture_site) for i in issues))
    assert found == expected_paths


def test_site_level_findings(report: AuditReport, fixture_site: str) -> None:
    ids = {i.id for i in report.issues}
    assert {"sitemap-duplicate-urls", "sitemap-invalid-urls", "sitemap-url-disallowed"} <= ids
    broken = find(report, "broken-internal-link")[0]
    assert broken.details["target"] == f"{fixture_site}/contact.html"
    assert broken.details["status"] == 404


def test_no_false_positives(report: AuditReport) -> None:
    ids = {i.id for i in report.issues}
    # robots.txt and the sitemap exist and are valid XML.
    assert not ids & {
        "robots-txt-missing",
        "robots-disallow-all",
        "sitemap-missing",
        "sitemap-invalid",
    }
    # print.html duplicates the homepage title but canonicalizes to it: consolidated, not a duplicate.
    for issue in find(report, "duplicate-title"):
        assert not any(u.endswith("/print.html") for u in issue.affected_urls)
    # Decorative images (alt="") must never be reported as missing alt text.
    for issue in find(report, "image-missing-alt"):
        assert not any(u.endswith("/about.html") for u in issue.affected_urls)
    # The fixture is served over plain HTTP on localhost: informational only.
    https = find(report, "https-not-used")
    assert https and all(i.severity == "info" for i in https)
    # Nothing beyond Phase 01 scope is invented.
    assert not any(i.id.startswith(("geo", "aeo", "backlink")) for i in report.issues)


def test_crawl_respects_robots_and_same_origin(report: AuditReport, fixture_site: str) -> None:
    crawled = {p.requested_url for p in report.crawl.pages}
    assert f"{fixture_site}/private/secret.html" not in crawled
    assert report.crawl.skipped[f"{fixture_site}/private/secret.html"] == "robots-disallowed"
    assert all(u.startswith(fixture_site) for u in crawled)  # external link never crawled
    assert not report.crawl.limit_reached


def test_structured_data_inventory(report: AuditReport) -> None:
    assert report.structured_data_types == {"Organization": 1}


def test_score_is_computed_with_all_categories(report: AuditReport) -> None:
    assert report.score.scored
    assert report.score.overall is not None and 0 < report.score.overall < 100
    assert len(report.score.categories) == 6


def test_audit_is_deterministic(fixture_site: str, report: AuditReport) -> None:
    config = CrawlConfig(max_pages=20, fetch=FetchConfig(delay=0, allow_private=True))
    again = run_audit(f"{fixture_site}/", config)
    assert [(i.id, i.affected_urls) for i in again.issues] == [
        (i.id, i.affected_urls) for i in report.issues
    ]
    assert again.score.overall == report.score.overall


def test_crawl_limit(fixture_site: str) -> None:
    config = CrawlConfig(max_pages=2, fetch=FetchConfig(delay=0, allow_private=True))
    limited = run_audit(f"{fixture_site}/", config)
    assert len(limited.crawl.pages) == 2
    assert limited.crawl.limit_reached
    assert any(i.id == "crawl-limit-reached" for i in limited.issues)


def test_cli_writes_json_and_markdown_reports(fixture_site: str, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "audit",
            f"{fixture_site}/",
            "--allow-private-network",
            "--delay",
            "0",
            "--output",
            str(tmp_path),
            "--quiet",
        ],
    )
    assert result.exit_code == 0, result.output
    directories = list(tmp_path.iterdir())
    assert len(directories) == 1 and directories[0].name.startswith("127.0.0.1_")
    data = json.loads((directories[0] / "audit.json").read_text(encoding="utf-8"))
    assert data["crawl"]["pages_audited"] >= 7
    assert any(i["id"] == "broken-internal-link" for i in data["issues"])
    markdown = (directories[0] / "audit.md").read_text(encoding="utf-8")
    assert "# Web Visibility Audit" in markdown and "Broken internal link" in markdown


def test_cli_json_to_stdout(fixture_site: str) -> None:
    result = CliRunner().invoke(
        app,
        [
            "audit",
            f"{fixture_site}/",
            "--allow-private-network",
            "--delay",
            "0",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["schema_version"] == "2.1"


def test_cli_terminal_report(fixture_site: str) -> None:
    result = CliRunner().invoke(
        app,
        [
            "audit",
            f"{fixture_site}/",
            "--allow-private-network",
            "--delay",
            "0",
            "--max-pages",
            "5",
        ],
    )
    assert result.exit_code == 0
    assert "WEB VISIBILITY AUDIT" in result.stdout
    assert "Diagnostic score only" in result.stdout


def test_unreachable_start_url_is_reported_not_raised() -> None:
    config = CrawlConfig(fetch=FetchConfig(delay=0, timeout=2, allow_private=True))
    unreachable = run_audit("http://127.0.0.1:9/", config)  # port 9 (discard) is closed
    assert not unreachable.auditable
    assert any(i.id == "start-url-unavailable" for i in unreachable.issues)


# Golden expectation: every finding on the fixture site, with the pages it affects.
# Reviewed by hand. A change here means behaviour changed - update deliberately.
GOLDEN = {
    ("broken-internal-link", ("/",)),
    ("crawled-page-error", ("/contact.html",)),
    ("missing-title", ("/services.html",)),
    ("duplicate-title", ("/about.html", "/duplicate.html")),
    ("image-missing-alt", ("/",)),
    ("invalid-json-ld", ("/services.html",)),
    ("sitemap-url-error", ("/gone.html",)),
    ("sitemap-url-noindex", ("/noindex.html",)),
    ("sitemap-url-disallowed", ()),
    ("duplicate-meta-description", ("/duplicate.html", "/services.html")),
    ("empty-anchor-text", ("/services.html",)),
    ("empty-heading", ("/services.html",)),
    ("heading-level-skip", ("/services.html",)),
    ("image-missing-dimensions", ("/",)),
    ("image-missing-dimensions", ("/about.html",)),
    ("image-missing-dimensions", ("/services.html",)),
    ("internal-link-redirect", ("/",)),
    ("missing-canonical", ("/services.html",)),
    ("missing-meta-description", ("/about.html",)),
    ("multiple-h1", ("/about.html",)),
    ("potential-orphan-page", ("/orphan.html",)),
    ("sitemap-url-redirects", ("/old-page",)),
    ("sitemap-duplicate-urls", ()),
    ("sitemap-invalid-urls", ()),
    ("canonicalized-to-other-url", ("/print.html",)),
    ("generic-anchor-text", ("/",)),
    ("https-not-used", ("/",)),
    ("image-empty-alt", ("/about.html",)),
    ("image-empty-alt", ("/services.html",)),
    ("noindex-page", ("/noindex.html",)),
    ("page-not-in-sitemap", ("/duplicate.html",)),
    ("linked-url-disallowed", ()),
}


def test_golden_findings(report: AuditReport, fixture_site: str) -> None:
    actual = {
        (i.id, tuple(sorted(u.removeprefix(fixture_site) for u in i.affected_urls)))
        for i in report.issues
    }
    missing, unexpected = GOLDEN - actual, actual - GOLDEN
    assert not missing and not unexpected, (
        f"missing={sorted(missing)} unexpected={sorted(unexpected)}"
    )
    assert len(report.issues) == len(GOLDEN)


def test_golden_score_and_coverage(report: AuditReport) -> None:
    assert report.crawl.state == "complete"
    assert report.score.status == "complete"
    assert report.score.overall == 82
    points = {c.category.value: c.points for c in report.score.categories}
    assert points == {
        "technical": 18.3, "metadata": 14.0, "structure": 14.1,
        "links": 17.2, "images": 9.1, "structured_data": 9.4,
    }  # fmt: skip


def test_golden_link_coverage_counts_targets_only(report: AuditReport) -> None:
    links = next(c for c in report.score.categories if c.category == "links")
    # 8 distinct internal targets checked; failing: /contact.html (404), /old-page (redirect).
    assert (links.coverage.applicable, links.coverage.checked, links.coverage.failed) == (8, 8, 2)
