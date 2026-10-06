"""Regression tests for the Phase 01 hardening audit (findings C1-L3).

Every test drives the REAL pipeline - controlled HTTP transport -> crawler ->
parser -> analyzers -> scoring - because the original flaws survived isolated
unit tests. Assertions are semantic (exact issue IDs, key fields), not byte
snapshots, so they catch behavioural regressions without being brittle.
"""

from __future__ import annotations

import gzip
import tracemalloc
import zlib
from pathlib import Path

import httpx
import pytest

from web_visibility.analyzers import RENDER_DEPENDENT_CONFIDENCE
from web_visibility.analyzers.crawlability import crawler_access_summary
from web_visibility.models import Severity

from fake_site import HTML, TEXT, XML, FakeSite, Route, audit, find, issue_ids, one, page

pytestmark = pytest.mark.integration

SPA = Path(__file__).parents[1] / "fixtures" / "spa"


def robots(text: str) -> Route:
    return Route(200, text, dict(TEXT))


def urlset(*urls: str) -> Route:
    entries = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return Route(200, f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>',
                 dict(XML))  # fmt: skip


def access(report, crawler: str) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return next(r for r in crawler_access_summary(report.context) if r["crawler"] == crawler)


# =========================================================================== #
# C1 - robots.txt crawler-specific rules
# =========================================================================== #


class TestC1RobotsPerCrawler:
    def test_case_a_googlebot_only_block_is_detected(self) -> None:
        report = audit(FakeSite({"/robots.txt": robots("User-agent: Googlebot\nDisallow: /\n"),
                                 "/": page()}))  # fmt: skip
        blocked = one(report, "robots-search-crawler-blocked")
        assert blocked.details["crawler"] == "Googlebot"
        assert "User-agent: googlebot -> Disallow: /" in blocked.evidence
        assert access(report, "Googlebot")["root_allowed"] is False
        assert access(report, "Bingbot")["root_allowed"] is True
        assert "robots-disallow-all" not in issue_ids(report)

    def test_case_b_wildcard_block_with_googlebot_exemption(self) -> None:
        text = "User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n"
        report = audit(FakeSite({"/robots.txt": robots(text), "/": page()}), respect_robots=False)
        assert access(report, "Googlebot")["root_allowed"] is True
        assert access(report, "Googlebot")["explicit_group"] is True
        assert "robots-disallow-all" not in issue_ids(report)  # not "all crawlers blocked"
        assert one(report, "robots-search-crawler-blocked").details["crawler"] == "Bingbot"

    def test_case_c_wildcard_block_blocks_all_major_crawlers(self) -> None:
        report = audit(FakeSite({"/robots.txt": robots("User-agent: *\nDisallow: /\n"),
                                 "/": page()}), respect_robots=False)  # fmt: skip
        assert one(report, "robots-disallow-all").severity is Severity.CRITICAL
        ai = one(report, "robots-ai-crawler-blocked")
        assert ai.severity is Severity.INFO  # AI blocking is a policy choice, not a defect
        assert {c["crawler"] for c in ai.details["crawlers"]} >= {"GPTBot", "Google-Extended"}
        assert all(not r["root_allowed"] for r in crawler_access_summary(report.context))

    def test_case_d_multiple_groups_and_merging(self) -> None:
        text = (
            "User-agent: Googlebot\nUser-agent: Bingbot\nDisallow: /private/\n\n"
            "User-agent: GPTBot\nDisallow: /\n\n"
            "User-agent: googlebot\nDisallow: /drafts/\n\n"  # same agent again: merged
            "User-agent: *\nDisallow: /\n"
        )
        report = audit(FakeSite({"/robots.txt": robots(text), "/": page()}), respect_robots=False)
        rules = report.crawl.robots.rules
        assert rules is not None
        assert not rules.is_allowed("/drafts/x", "Googlebot")
        assert not rules.is_allowed("/private/x", "Googlebot")
        assert rules.is_allowed("/drafts/x", "Bingbot")
        assert access(report, "GPTBot")["root_allowed"] is False
        assert access(report, "ClaudeBot")["root_allowed"] is False  # falls back to *
        assert "robots-disallow-all" not in issue_ids(report)
        assert "robots-search-crawler-blocked" not in issue_ids(report)

    def test_case_e_empty_disallow_allows_everything(self) -> None:
        text = "User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nDisallow:\n"
        report = audit(FakeSite({"/robots.txt": robots(text), "/": page()}), respect_robots=False)
        assert access(report, "Googlebot")["root_allowed"] is True
        assert access(report, "Googlebot")["rule"] is None

    def test_case_f_longest_match_and_allow_wins_ties(self) -> None:
        text = (
            "User-agent: Googlebot\nDisallow: /shop\nAllow: /shop/public\nDisallow: /x\nAllow: /x\n"
        )
        report = audit(FakeSite({"/robots.txt": robots(text), "/": page()}))
        rules = report.crawl.robots.rules
        assert rules is not None
        assert not rules.is_allowed("/shop/cart", "Googlebot")
        assert rules.is_allowed("/shop/public/item", "Googlebot")
        assert rules.is_allowed("/x", "Googlebot")  # equal length: Allow wins

    def test_version_suffix_and_case_in_user_agent_lines(self) -> None:
        report = audit(FakeSite({"/robots.txt": robots("User-Agent: GOOGLEBOT/2.1\nDisallow: /\n"),
                                 "/": page()}))  # fmt: skip
        assert access(report, "Googlebot")["root_allowed"] is False

    def test_start_url_blocked_for_one_crawler_only(self) -> None:
        text = "User-agent: Bingbot\nDisallow: /$\n"
        report = audit(FakeSite({"/robots.txt": robots(text), "/": page()}))
        issue = one(report, "start-url-disallowed")
        assert [c["crawler"] for c in issue.details["crawlers"]] == ["Bingbot"]

    def test_auditor_obeys_its_own_group(self) -> None:
        text = "User-agent: WebVisibilitySkill\nDisallow: /\n\nUser-agent: *\nDisallow:\n"
        report = audit(FakeSite({"/robots.txt": robots(text), "/": page()}))
        assert report.crawl.state == "failed"
        assert report.crawl.skipped["https://example.com/"] == "robots-disallowed"
        assert "robots-disallow-all" not in issue_ids(report)  # search crawlers are allowed


# =========================================================================== #
# C2 - client-rendered JavaScript shells
# =========================================================================== #


class TestC2ClientRenderedShell:
    @pytest.mark.parametrize("fixture", ["vite-shell.html", "custom-mount-shell.html"])
    def test_shell_detected_and_absence_findings_downgraded(self, fixture: str) -> None:
        report = audit(FakeSite({"/": Route(200, (SPA / fixture).read_bytes())}))
        shell = one(report, "client-rendered-shell")
        assert shell.confidence >= 0.9
        for rule_id in ("missing-h1", "missing-canonical", "no-internal-outlinks"):
            issue = one(report, rule_id)
            assert issue.confidence <= RENDER_DEPENDENT_CONFIDENCE
            assert issue.details["render_dependent"] is True
        assert report.score.status == "partial"

    def test_custom_mount_id_detected_without_root_id(self) -> None:
        report = audit(FakeSite({"/": Route(200, (SPA / "custom-mount-shell.html").read_bytes())}))
        signals = report.context.pages[0].content.render_signals  # type: ignore[union-attr]
        assert not any("#root" in s for s in signals)
        assert any(s.startswith("noscript-javascript-notice") for s in signals)

    @pytest.mark.parametrize(
        "body",
        [
            (SPA / "static-minimal.html").read_bytes(),
            (Path(__file__).parents[1] / "fixtures" / "site" / "about.html").read_bytes(),
        ],
    )
    def test_static_pages_are_not_flagged(self, body: bytes) -> None:
        report = audit(
            FakeSite({"/": Route(200, body.replace(b"{{BASE}}", b"https://example.com"))})
        )
        assert "client-rendered-shell" not in issue_ids(report)
        h1 = find(report, "missing-h1")
        assert all(i.confidence > RENDER_DEPENDENT_CONFIDENCE for i in h1)

    def test_shell_wording_does_not_claim_seo_is_broken(self) -> None:
        report = audit(FakeSite({"/": Route(200, (SPA / "vite-shell.html").read_bytes())}))
        text = (one(report, "client-rendered-shell").description + one(
            report, "client-rendered-shell").recommendation).lower()  # fmt: skip
        assert "browser rendering may be required" in text
        assert "broken" not in text


# =========================================================================== #
# H1 - decompression and response-size limits
# =========================================================================== #


class TestH1DecompressionLimits:
    def test_gzip_bomb_is_bounded_in_memory_and_reported(self) -> None:
        bomb = gzip.compress(b"<html><body>" + b"A" * 40_000_000)  # ~40 MB inflated, ~40 KB raw
        site = FakeSite({"/": Route(200, bomb, {**HTML, "content-encoding": "gzip"}),
                         "/other": page()})  # fmt: skip
        tracemalloc.start()
        report = audit(site, fetch={"max_bytes": 1_000_000})
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 15_000_000, f"peak memory {peak / 1e6:.1f} MB grew with the payload"
        start = report.crawl.pages[0]
        assert start.error_kind == "too-large" and start.content is None
        assert report.crawl.state == "failed"  # an unanalysed start page is not an audited site

    def test_deflate_bomb_on_secondary_page_does_not_stop_the_audit(self) -> None:
        bomb = zlib.compress(b"<p>" + b"B" * 20_000_000)
        site = FakeSite({"/": page(links=("/huge",)),
                         "/huge": Route(200, bomb, {**HTML, "content-encoding": "deflate"})})  # fmt: skip
        report = audit(site, fetch={"max_bytes": 500_000})
        assert one(report, "page-too-large").affected_urls == ("https://example.com/huge",)
        assert report.crawl.state == "complete"

    def test_compressed_normal_page_is_decoded(self) -> None:
        html = page(title="Compressed page title here").body
        assert isinstance(html, str)
        site = FakeSite(
            {"/": Route(200, gzip.compress(html.encode()), {**HTML, "content-encoding": "gzip"})}
        )
        report = audit(site)
        assert report.context.pages[0].title == "Compressed page title here"

    def test_unrequested_encoding_is_an_error_not_garbage(self) -> None:
        site = FakeSite({"/": Route(200, b"\x8b\x00garbage", {**HTML, "content-encoding": "br"})})
        report = audit(site)
        assert report.crawl.pages[0].error_kind == "unsupported-encoding"


# =========================================================================== #
# H2 - crawler-specific noindex
# =========================================================================== #


class TestH2CrawlerSpecificNoindex:
    @pytest.mark.parametrize(
        ("head", "headers", "expected"),
        [
            ('<meta name="robots" content="noindex">', {}, ["all crawlers"]),
            ('<meta name="googlebot" content="noindex">', {}, ["googlebot"]),
            ('<meta name="bingbot" content="none">', {}, ["bingbot"]),
            ("", {"x-robots-tag": "noindex"}, ["all crawlers"]),
            ("", {"x-robots-tag": "googlebot: noindex"}, ["googlebot"]),
        ],
    )
    def test_noindex_sources(self, head: str, headers: dict[str, str], expected: list[str]) -> None:
        route = page(head=head, canonical="https://example.com/")
        route.headers.update(headers)
        report = audit(FakeSite({"/": route}))
        assert one(report, "noindex-start-page").details["noindex_for"] == expected

    def test_robots_allows_but_googlebot_blocks(self) -> None:
        head = (
            '<meta name="robots" content="index, follow"><meta name="googlebot" content="noindex">'
        )
        report = audit(FakeSite({"/": page(head=head, canonical="https://example.com/")}))
        issue = one(report, "noindex-start-page")
        assert (
            issue.description == "Googlebot is explicitly instructed not to index the start page."
        )
        assert "cannot appear" not in issue.description
        assert report.context.pages[0].is_noindex is False

    def test_other_agent_scope_does_not_leak(self) -> None:
        route = page(canonical="https://example.com/")
        route.headers["x-robots-tag"] = "otherbot: noindex, nofollow"
        report = audit(FakeSite({"/": route}))
        assert "noindex-start-page" not in issue_ids(report)
        assert "nofollow-page" not in issue_ids(report)


# =========================================================================== #
# H3 - scoring must not reward unchecked data
# =========================================================================== #


def _links(n: int, prefix: str = "/p") -> tuple[str, ...]:
    return tuple(f"{prefix}{i}" for i in range(n))


class TestH3CoverageAwareScoring:
    def test_thirty_unchecked_links_are_not_scored(self) -> None:
        site = FakeSite({"/": page(links=_links(30), canonical="https://example.com/")})
        report = audit(site, max_pages=1, max_link_checks=0)
        links = next(c for c in report.score.categories if c.category == "links")
        assert links.points is None
        assert links.coverage.status == "not-scored"
        assert (links.coverage.applicable, links.coverage.checked) == (30, 0)
        assert report.score.status == "partial"
        assert report.score.scored_weight < 100

    def test_partial_link_coverage(self) -> None:
        routes: dict[str, object] = {"/": page(links=_links(30), canonical="https://example.com/")}
        report = audit(FakeSite(routes), max_pages=1, max_link_checks=10)  # type: ignore[arg-type]
        links = next(c for c in report.score.categories if c.category == "links")
        assert links.coverage.status == "partial"
        assert (links.coverage.applicable, links.coverage.checked) == (30, 10)
        assert links.points is not None and links.points < 15  # the 10 checked are all 404

    def test_full_link_coverage_with_healthy_links(self) -> None:
        routes = {"/": page(links=_links(5), canonical="https://example.com/")}
        routes.update({f"/p{i}": page(title=f"Healthy page number {i}", links=("/",),
                                      canonical=f"https://example.com/p{i}") for i in range(5)})  # fmt: skip
        report = audit(FakeSite(routes))  # type: ignore[arg-type]
        links = next(c for c in report.score.categories if c.category == "links")
        assert links.coverage.status == "scored" and links.points == 20

    def test_nineteen_of_twenty_pages_404(self) -> None:
        site = FakeSite({"/": page(links=_links(19), canonical="https://example.com/")})
        report = audit(site)
        error = one(report, "crawled-page-error")
        assert error.severity is Severity.CRITICAL  # escalated: most of the site is broken
        assert len(error.affected_urls) == 19
        broken = find(report, "broken-internal-link")
        assert len(broken) == 19
        categories = {c.category.value: c for c in report.score.categories}
        assert categories["links"].points is not None and categories["links"].points < 8
        assert categories["technical"].points is not None and categories["technical"].points < 8
        assert report.score.overall is not None
        assert 20 <= report.score.overall < 65  # severe, transparent, but not absurdly zero

    def test_zero_checked_items_when_crawl_fails(self) -> None:
        report = audit(FakeSite({"/": Route(503, "down")}))
        assert report.crawl.state == "failed"
        assert report.score.overall is None
        assert all(c.points is None for c in report.score.categories)


# =========================================================================== #
# H5 - guessed sitemap location
# =========================================================================== #


class TestH5GuessedSitemap:
    def test_spa_fallback_at_guess_is_ignored_when_declared_sitemap_is_valid(self) -> None:
        site = FakeSite({
            "/robots.txt": robots("Sitemap: https://example.com/sitemap-index.xml\n"),
            "/sitemap-index.xml": urlset("https://example.com/"),
            "/sitemap.xml": Route(200, (SPA / "vite-shell.html").read_bytes()),
            "/": page(canonical="https://example.com/"),
        })  # fmt: skip
        report = audit(site)
        assert not {i for i in issue_ids(report) if i.startswith("sitemap-")} - {
            "sitemap-not-in-robots"
        }
        guess = next(f for f in report.crawl.sitemaps.files if f.guessed)
        assert guess.looks_like_html
        assert report.crawl.sitemaps.complete

    def test_no_declared_sitemap_and_html_guess_is_conservative(self) -> None:
        site = FakeSite({"/sitemap.xml": Route(200, (SPA / "vite-shell.html").read_bytes()),
                         "/": page(canonical="https://example.com/")})  # fmt: skip
        report = audit(site)
        assert "sitemap-invalid" not in issue_ids(report)
        missing = one(report, "sitemap-missing")
        assert missing.severity is Severity.LOW
        assert "an HTML page, not a sitemap" in missing.evidence

    def test_guessed_location_with_broken_xml_is_reported(self) -> None:
        site = FakeSite({"/sitemap.xml": Route(200, "<urlset><url><loc>x", dict(XML)),
                         "/": page(canonical="https://example.com/")})  # fmt: skip
        assert one(audit(site), "sitemap-invalid").confidence < 0.95


# =========================================================================== #
# M1 - robots.txt response handling
# =========================================================================== #


class TestM1RobotsResponses:
    @pytest.mark.parametrize(
        ("status", "state", "rule", "crawls"),
        [
            (404, "missing", "robots-txt-missing", True),
            (403, "inaccessible", "robots-txt-access-denied", True),
            (429, "rate-limited", "robots-txt-rate-limited", False),
            (503, "server-error", "robots-txt-unreachable", False),
        ],
    )
    def test_status_mapping(self, status: int, state: str, rule: str, crawls: bool) -> None:
        site = FakeSite({"/robots.txt": Route(status, "x", dict(TEXT)),
                         "/": page(canonical="https://example.com/")})  # fmt: skip
        report = audit(site)
        assert report.crawl.robots.state == state
        assert rule in issue_ids(report)
        assert (report.crawl.state != "failed") is crawls

    def test_timeout_is_retried_once(self) -> None:
        attempts = {"n": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ReadTimeout("slow", request=request)
            return robots("User-agent: *\nDisallow:\n").response()

        report = audit(
            FakeSite({"/robots.txt": flaky, "/": page(canonical="https://example.com/")})
        )
        assert attempts["n"] == 2
        assert report.crawl.robots.state == "parsed"
        assert report.crawl.state == "complete"

    def test_persistent_network_failure_is_an_incomplete_audit_not_an_empty_site(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("unreachable", request=request)

        report = audit(FakeSite({"/robots.txt": down, "/": down}))
        assert report.crawl.state == "failed"
        failed = one(report, "start-url-unavailable")
        assert failed.confidence <= 0.6  # temporary failure, not established fact
        assert "incomplete audit" in failed.description
        assert report.score.overall is None
        assert "sitemap-missing" not in issue_ids(report)  # nothing checked, no claim


# =========================================================================== #
# M2 - canonical detection
# =========================================================================== #


class TestM2Canonicals:
    def _audit_head(self, head: str, *, body: str = "", headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        route = page(head=head, body=body)
        route.headers.update(headers or {})
        return audit(FakeSite({"/": route}))

    def test_html_head_canonical(self) -> None:
        report = self._audit_head('<link rel="canonical" href="https://example.com/">')
        assert not {i for i in issue_ids(report) if "canonical" in i}

    def test_http_header_canonical(self) -> None:
        report = self._audit_head("", headers={"link": '<https://example.com/>; rel="canonical"'})
        assert "missing-canonical" not in issue_ids(report)
        assert report.context.pages[0].canonical == "https://example.com/"

    def test_body_canonical_is_not_trusted(self) -> None:
        report = self._audit_head("", body='<link rel="canonical" href="https://example.com/">')
        misplaced = one(report, "canonical-outside-head")
        assert misplaced.details["placement"] == "body"
        assert "missing-canonical" not in issue_ids(report)  # reported once, as placement
        assert report.context.pages[0].canonical is None

    def test_multiple_conflicting_canonicals(self) -> None:
        report = self._audit_head(
            '<link rel="canonical" href="/a"><link rel="canonical" href="/b">'
        )
        assert one(report, "multiple-canonicals").severity is Severity.MEDIUM

    def test_head_implicitly_closed_by_body_content(self) -> None:
        report = self._audit_head(
            '<div>banner</div><link rel="canonical" href="https://example.com/">'
        )
        issue = one(report, "canonical-outside-head")
        assert issue.details["placement"] == "head-implicitly-closed"
        assert issue.confidence <= 0.5  # parser behaviour is ambiguous; do not overclaim

    def test_header_and_html_disagree(self) -> None:
        report = self._audit_head('<link rel="canonical" href="https://example.com/">',
                                  headers={"link": '<https://example.com/other>; rel="canonical"'})  # fmt: skip
        assert "canonical-header-conflict" in issue_ids(report)


# =========================================================================== #
# M3 - sitemap completeness
# =========================================================================== #


class TestM3SitemapCompleteness:
    def test_file_limit_disables_completeness_findings(self) -> None:
        index = "".join(
            f"<sitemap><loc>https://example.com/s{i}.xml</loc></sitemap>" for i in range(5)
        )
        routes = {
            "/robots.txt": robots("Sitemap: https://example.com/index.xml\n"),
            "/index.xml": Route(200, f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{index}</sitemapindex>', dict(XML)),
            "/s0.xml": urlset("https://example.com/"),
            "/": page(links=("/unlisted",), canonical="https://example.com/"),
            "/unlisted": page(title="An unlisted but indexable page", links=("/",),
                              canonical="https://example.com/unlisted"),
        }  # fmt: skip
        report = audit(FakeSite(routes), max_sitemaps=2)  # type: ignore[arg-type]
        assert report.crawl.sitemaps.complete is False
        assert "sitemap-incomplete" in issue_ids(report)
        assert "page-not-in-sitemap" not in issue_ids(report)

    def test_complete_sitemap_enables_not_in_sitemap(self) -> None:
        routes = {
            "/robots.txt": robots("Sitemap: https://example.com/s.xml\n"),
            "/s.xml": urlset("https://example.com/"),
            "/": page(links=("/unlisted",), canonical="https://example.com/"),
            "/unlisted": page(title="An unlisted but indexable page", links=("/",),
                              canonical="https://example.com/unlisted"),
        }  # fmt: skip
        report = audit(FakeSite(routes))  # type: ignore[arg-type]
        assert one(report, "page-not-in-sitemap").affected_urls == ("https://example.com/unlisted",)

    def test_empty_child_sitemaps_are_aggregated(self) -> None:
        index = "".join(
            f"<sitemap><loc>https://example.com/e{i}.xml</loc></sitemap>" for i in range(4)
        )
        routes = {
            "/robots.txt": robots("Sitemap: https://example.com/index.xml\n"),
            "/index.xml": Route(200, f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{index}</sitemapindex>', dict(XML)),
            **{f"/e{i}.xml": urlset() for i in range(4)},
            "/": page(canonical="https://example.com/"),
        }  # fmt: skip
        report = audit(FakeSite(routes))  # type: ignore[arg-type]
        empty = one(report, "sitemap-empty")
        assert len(empty.details["files"]) == 4

    def test_noindex_and_not_in_sitemap_never_contradict(self) -> None:
        routes = {
            "/robots.txt": robots("Sitemap: https://example.com/s.xml\n"),
            "/s.xml": urlset("https://example.com/"),
            "/": page(links=("/thanks", "/print"), canonical="https://example.com/"),
            "/thanks": page(title="Thanks for your order today",
                            head='<meta name="robots" content="noindex">', links=("/",)),
            "/print": page(title="Printable version of home", links=("/",),
                           canonical="https://example.com/"),
        }  # fmt: skip
        report = audit(FakeSite(routes))  # type: ignore[arg-type]
        assert "page-not-in-sitemap" not in issue_ids(report)


# =========================================================================== #
# M4 - XML security
# =========================================================================== #


class TestM4XmlSecurity:
    @pytest.mark.parametrize(
        "body",
        [
            '<?xml version="1.0"?><!DOCTYPE urlset><urlset/>',
            "<!--" + "x" * 10_000 + '--><!DOCTYPE urlset [<!ENTITY a "b">]><urlset/>',
            '<?xml version="1.0"?><!DOCTYPE u [<!ENTITY x SYSTEM "http://169.254.169.254/">]><urlset><url><loc>&x;</loc></url></urlset>',
            '<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><urlset><url><loc>&b;</loc></url></urlset>',
        ],
        ids=["doctype", "doctype-after-large-comment", "external-entity", "entity-expansion"],
    )
    def test_unsafe_xml_is_rejected_and_never_fetched(self, body: str) -> None:
        site = FakeSite({"/robots.txt": robots("Sitemap: https://example.com/s.xml\n"),
                         "/s.xml": Route(200, body, dict(XML)), "/": page()})  # fmt: skip
        report = audit(site)
        invalid = one(report, "sitemap-invalid")
        assert "unsafe XML rejected" in invalid.evidence
        assert all(r.url.host == "example.com" for r in site.requests)  # no entity resolution

    def test_malformed_xml(self) -> None:
        site = FakeSite({"/robots.txt": robots("Sitemap: https://example.com/s.xml\n"),
                         "/s.xml": Route(200, "<urlset><url>", dict(XML)), "/": page()})  # fmt: skip
        assert "invalid XML" in one(audit(site), "sitemap-invalid").evidence

    def test_oversized_sitemap_fails_safely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from web_visibility import crawler

        monkeypatch.setattr(crawler, "MAX_SITEMAP_BYTES", 10_000)
        body = urlset(*(f"https://example.com/p{i}" for i in range(2000))).body
        site = FakeSite({"/robots.txt": robots("Sitemap: https://example.com/s.xml\n"),
                         "/s.xml": Route(200, body, dict(XML)), "/": page()})  # fmt: skip
        assert "larger than" in one(audit(site), "sitemap-invalid").evidence


# =========================================================================== #
# M8 - Retry-After and Crawl-delay
# =========================================================================== #


class TestM8RateControl:
    def test_retry_after_is_honoured_once(self) -> None:
        attempts = {"n": 0}

        def limited(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "5"})
            return page(canonical="https://example.com/").response()

        sleeps: list[float] = []
        report = audit(FakeSite({"/": limited}), sleeps=sleeps)
        assert attempts["n"] == 2
        assert 5.0 in sleeps  # waited (on the fake clock) exactly as asked
        assert report.crawl.state == "complete"

    def test_excessive_retry_after_stops_the_crawl(self) -> None:
        site = FakeSite({"/": page(links=_links(5), canonical="https://example.com/"),
                         "/p0": Route(429, "", {"retry-after": "3600"})})  # fmt: skip
        sleeps: list[float] = []
        report = audit(site, sleeps=sleeps)
        assert 3600.0 not in sleeps
        assert report.crawl.stop_reason == "rate-limited"
        assert "crawl-stopped-early" in issue_ids(report)
        assert "/p1" not in site.paths()  # backed off instead of continuing

    def test_503_retry_after(self) -> None:
        attempts = {"n": 0}

        def unavailable(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503, headers={"retry-after": "2"})
            return page(canonical="https://example.com/").response()

        assert audit(FakeSite({"/": unavailable})).crawl.state == "complete"

    def test_crawl_delay_is_applied(self) -> None:
        site = FakeSite({"/robots.txt": robots("User-agent: *\nCrawl-delay: 2\n"),
                         "/": page(links=("/a",), canonical="https://example.com/"),
                         "/a": page(title="Second page title text", links=("/",))})  # fmt: skip
        sleeps: list[float] = []
        report = audit(site, sleeps=sleeps)
        assert report.crawl.crawl_delay == 2.0
        assert sleeps and max(sleeps) <= 2.0 and sum(sleeps) >= 2.0

    def test_crawl_delay_above_limit_fetches_only_the_start_page(self) -> None:
        site = FakeSite({"/robots.txt": robots("User-agent: *\nCrawl-delay: 120\n"),
                         "/": page(links=("/a",), canonical="https://example.com/")})  # fmt: skip
        report = audit(site)
        assert len(report.crawl.pages) == 1
        assert report.crawl.stop_reason == "crawl-delay-too-large"


# =========================================================================== #
# L1 / L2 - same-site adoption and robots percent-encoding
# =========================================================================== #


class TestLowFindings:
    def test_subdomain_redirect_is_not_adopted(self) -> None:
        site = FakeSite({"example.com/": Route(301, "", {"location": "https://shop.example.com/"}),
                         "shop.example.com/": page()})  # fmt: skip
        report = audit(site)
        assert report.crawl.site_url == "https://example.com/"
        assert report.crawl.state == "failed"
        assert "different site" in one(report, "start-url-unavailable").evidence

    def test_www_redirect_is_adopted(self) -> None:
        site = FakeSite({"example.com/": Route(301, "", {"location": "https://www.example.com/"}),
                         "www.example.com/": page(canonical="https://www.example.com/")})  # fmt: skip
        assert audit(site).crawl.site_url == "https://www.example.com/"

    @pytest.mark.parametrize(("rule", "path"), [("/%7Ejoe/", "/~joe/x"), ("/~joe/", "/%7Ejoe/x")])
    def test_robots_percent_encoding_equivalence(self, rule: str, path: str) -> None:
        site = FakeSite({"/robots.txt": robots(f"User-agent: *\nDisallow: {rule}\n"),
                         "/": page(links=(path,), canonical="https://example.com/")})  # fmt: skip
        report = audit(site)
        assert "https://example.com/~joe/x" in report.crawl.skipped


# =========================================================================== #
# Score sanity matrix (real pipeline)
# =========================================================================== #


def _healthy_site(pages: int = 4) -> dict[str, object]:
    sitemap = urlset("https://example.com/", *(f"https://example.com/p{i}" for i in range(pages)))
    routes: dict[str, object] = {
        "/robots.txt": robots("User-agent: *\nDisallow:\nSitemap: https://example.com/s.xml\n"),
        "/s.xml": sitemap,
        "/": page(title="Home of the example company", links=_links(pages),
                  canonical="https://example.com/",
                  head='<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization"}</script>',
                  body='<img src="/logo.png" alt="Company logo" width="10" height="10">'),
    }  # fmt: skip
    for i in range(pages):
        routes[f"/p{i}"] = page(title=f"Detailed service page number {i}", links=("/",),
                                canonical=f"https://example.com/p{i}")  # fmt: skip
    return routes


class TestScoreSanity:
    def test_perfect_site_scores_high(self) -> None:
        report = audit(FakeSite(_healthy_site()))  # type: ignore[arg-type]
        assert report.score.status == "complete"
        assert report.score.overall is not None and report.score.overall >= 95

    def test_minor_issues_score_moderately_high(self) -> None:
        routes = _healthy_site()
        routes["/p0"] = page(title="Detailed service page number 0", links=("/",))  # no canonical
        routes["/p1"] = page(title="Detailed service page number 1", links=("/",),
                             canonical="https://example.com/p1", body='<img src="/x.png">')  # fmt: skip
        report = audit(FakeSite(routes))  # type: ignore[arg-type]
        perfect = audit(FakeSite(_healthy_site())).score.overall  # type: ignore[arg-type]
        assert report.score.overall is not None and perfect is not None
        assert 85 <= report.score.overall < perfect

    def test_many_issues_score_substantially_lower(self) -> None:
        routes = _healthy_site()
        bad = "<!doctype html><html><head></head><body><h3></h3><img src='/a.png'><a href='/gone'></a></body></html>"
        for i in range(4):
            routes[f"/p{i}"] = Route(200, bad)
        report = audit(FakeSite(routes))  # type: ignore[arg-type]
        perfect = audit(FakeSite(_healthy_site())).score.overall  # type: ignore[arg-type]
        assert report.score.overall is not None and perfect is not None
        assert report.score.overall <= perfect - 20

    def test_catastrophic_crawl_failure_is_not_scored(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        report = audit(FakeSite({"/robots.txt": down, "/": down}))
        assert report.score.overall is None and report.crawl.state == "failed"


class TestPrecisionRefinements:
    def test_tiny_static_page_with_analytics_script_is_not_a_shell(self) -> None:
        html = ("<!doctype html><html><head><title>Example Domain</title></head><body>"
                "<p>Documentation examples only.</p><script src='/analytics.js'></script>"
                "</body></html>")  # fmt: skip
        report = audit(FakeSite({"/": Route(200, html)}))
        assert "client-rendered-shell" not in issue_ids(report)
        assert one(report, "missing-h1").confidence > RENDER_DEPENDENT_CONFIDENCE

    def test_nothing_to_check_is_not_applicable_and_audit_can_be_complete(self) -> None:
        report = audit(FakeSite({"/": page(canonical="https://example.com/")}))
        cats = {c.category.value: c for c in report.score.categories}
        for name in ("images", "links"):  # the page has neither images nor links
            assert cats[name].coverage.status == "not-applicable"
            assert cats[name].points is None
        assert report.score.status == "complete"
        assert report.score.scored_weight == 70
