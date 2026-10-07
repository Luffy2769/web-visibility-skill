"""Independent audit #2 probe matrices, kept as permanent regression tests.

Each table is the probe the auditor ran by hand, with the verdict that was
verified correct. Includes N6 (text budget false positives) and N8 (Crawl-delay
above the limit still sending link checks).
"""

from __future__ import annotations

import socket

import pytest

from web_visibility.config import CrawlConfig, FetchConfig
from web_visibility.crawler import Crawler
from web_visibility.fetch import Fetcher
from web_visibility.robots import RobotsTxt

from live_server import html_page, live_audit, live_server, send

pytestmark = pytest.mark.integration

AGENTS = ("Googlebot", "Bingbot", "GPTBot", "Google-Extended", "WebVisibilitySkill")


# --------------------------------------------------------------------------- #
# robots.txt semantics (RFC 9309) - expected verdicts per agent, in AGENTS order
# --------------------------------------------------------------------------- #

A, B = True, False  # allowed / blocked
ROBOTS_MATRIX = {
    "googlebot-only": ("User-agent: Googlebot\nDisallow: /\n", "/x", (B, A, A, A, A)),
    "star-block-googlebot-allow": ("User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n", "/x", (A, B, B, B, B)),
    "gptbot": ("User-agent: GPTBot\nDisallow: /\n", "/x", (A, A, B, A, A)),
    "google-extended": ("User-agent: Google-Extended\nDisallow: /\n", "/x", (A, A, A, B, A)),
    "bingbot": ("User-agent: Bingbot\nDisallow: /\n", "/x", (A, B, A, A, A)),
    "empty-disallow-specific": ("User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nDisallow:\n", "/x", (A, B, B, B, B)),
    "longest-allow": ("User-agent: *\nDisallow: /a\nAllow: /a/b\n", "/a/b/c", (A,) * 5),
    "longest-disallow": ("User-agent: *\nAllow: /a\nDisallow: /a/b\n", "/a/b/c", (B,) * 5),
    "tie-allow-wins": ("User-agent: *\nDisallow: /p\nAllow: /p\n", "/p", (A,) * 5),
    "end-anchor": ("User-agent: *\nDisallow: /*.pdf$\n", "/doc.pdf", (B,) * 5),
    "end-anchor-query": ("User-agent: *\nDisallow: /*.pdf$\n", "/doc.pdf?x=1", (A,) * 5),
    "encoded-rule": ("User-agent: *\nDisallow: /%7Ejoe\n", "/~joe/x", (B,) * 5),
    "encoded-url": ("User-agent: *\nDisallow: /~joe\n", "http://h/%7ejoe/x", (B,) * 5),
    "unicode-rule": ("User-agent: *\nDisallow: /café\n", "http://h/caf%C3%A9", (B,) * 5),
    "merged-groups": ("User-agent: googlebot\nDisallow: /a\n\nUser-agent: googlebot\nDisallow: /b\n", "/b", (B, A, A, A, A)),
    "multi-agent-group": ("User-agent: foo\nUser-agent: Googlebot\nDisallow: /\n", "/x", (B, A, A, A, A)),
    "case-insensitive-agent": ("USER-AGENT: GOOGLEBOT\nDISALLOW: /\n", "/x", (B, A, A, A, A)),
    "versioned-agent": ("User-agent: Googlebot/2.1\nDisallow: /\n", "/x", (B, A, A, A, A)),
    "googlebot-news-not-googlebot": ("User-agent: Googlebot-News\nDisallow: /\n", "/x", (A,) * 5),
    "case-sensitive-path": ("User-agent: *\nDisallow: /Admin\n", "/admin", (A,) * 5),
    "query-pattern": ("User-agent: *\nDisallow: /*?sort=\n", "/list?sort=asc", (B,) * 5),
    "home-only": ("User-agent: *\nDisallow: /$\n", "/x", (A,) * 5),
    "rule-before-any-agent": ("Disallow: /\nUser-agent: *\nAllow: /\n", "/x", (A,) * 5),
}  # fmt: skip


@pytest.mark.parametrize("case", list(ROBOTS_MATRIX))
def test_robots_semantics_matrix(case: str) -> None:
    text, path, expected = ROBOTS_MATRIX[case]
    rules = RobotsTxt.parse(text)
    assert tuple(rules.is_allowed(path, agent) for agent in AGENTS) == expected


def test_crawler_and_analyzer_agree_on_the_auditors_own_group() -> None:
    """The crawler obeys the auditor's own token; the analyzer reports per crawler."""
    with live_server() as srv:
        srv.page("/robots.txt", "User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n",
                 content_type="text/plain")  # fmt: skip
        srv.page("/", html_page("Home page title"))
        report = live_audit(srv)
    assert report.crawl.state == "failed"
    assert srv.hits("/") == 0  # never fetched what robots.txt disallows for us
    ids = {i.id for i in report.issues}
    assert "robots-search-crawler-blocked" in ids  # Bingbot
    assert "robots-disallow-all" not in ids  # Googlebot is allowed


# --------------------------------------------------------------------------- #
# JavaScript shells
# --------------------------------------------------------------------------- #

FILLER = "<p>" + "Real server-rendered paragraph text about the product. " * 6 + "</p>"
SCRIPTS = "".join(f'<script src="/static/chunk{i}.js"></script>' for i in range(30))
SHELLS = {
    "react-vite": ('<div id="root"></div><script type="module" src="/src/main.jsx"></script>', True),
    "vue": ('<div id="app"></div><script src="/assets/app.js"></script>', True),
    "cra-noscript": ('<noscript>You need to enable JavaScript to run this app.</noscript>'
                     '<div id="root"></div><script src="/static/js/main.js"></script>', True),
    "custom-mount": ('<div id="mount-point"></div><script src="/bundle.js"></script>', True),
    "minimal-static": ("<main><h1>Hello</h1></main>", False),
    "static-with-analytics": ('<main><h1>Hello</h1><p>Short.</p></main>'
                              '<script async src="https://example.org/gtag.js"></script>', False),
    "ssr-js-heavy": (f'<div id="__next"><main><h1>Product</h1>{FILLER}{FILLER}</main></div>{SCRIPTS}', False),
    "login-hydrated": ('<div id="root"><form><h1>Sign in</h1><label>Email</label><input>'
                       '<button>Continue</button></form></div><script type="module" src="/m.js"></script>', False),
}  # fmt: skip


@pytest.mark.parametrize("name", list(SHELLS))
def test_shell_detection_matrix(name: str) -> None:
    body, is_shell = SHELLS[name]
    with live_server() as srv:
        srv.page(
            "/",
            f"<!doctype html><html><head><title>Probe page title</title></head><body>{body}</body></html>",
        )
        report = live_audit(srv)
    page = report.context.start_page
    assert page is not None and page.likely_client_rendered is is_shell
    if is_shell:
        hedged = [i for i in report.issues if i.details.get("render_dependent")]
        assert {"missing-h1", "missing-canonical"} <= {i.id for i in hedged}
        assert report.score.overall is None  # N4: nothing page-level was measurable


# --------------------------------------------------------------------------- #
# N6 - bounded text must not read common markup as empty
# --------------------------------------------------------------------------- #


def test_logo_svg_in_heading_and_link_keeps_their_text() -> None:
    logo = "<svg viewBox='0 0 10 10'>" + "\n  <path d='M0 0h1v1z'/>" * 40 + "\n</svg>"
    with live_server() as srv:
        srv.page("/", html_page("Logo heading page", body=(
            f'<h1><a href="/about">{logo}<span class="sr-only">Acme Corporation</span></a></h1>'
            '<a href="/about"><svg><title>About us</title><path d="M0"/></svg></a>'
            '<h2><script>var x = "not heading text";</script>Real subheading</h2>'
        )))  # fmt: skip
        srv.page("/about", html_page("About page title"))
        report = live_audit(srv)
    page = report.context.start_page
    assert page is not None
    assert "Acme Corporation" in page.h1s
    assert [h.text for h in page.headings if h.level == 2] == ["Real subheading"]
    assert {link.text for link in page.links} >= {"Acme Corporation", "About us"}
    ids = {i.id for i in report.issues if page.final_url in i.affected_urls}
    assert not ids & {"empty-heading", "empty-anchor-text"}


# --------------------------------------------------------------------------- #
# Canonicals
# --------------------------------------------------------------------------- #

CANONICALS = {
    "head-absolute": ('<link rel="canonical" href="{BASE}/c">', "", set()),
    "head-relative": ('<link rel="canonical" href="c">', "", set()),
    "body": ("", '<link rel="canonical" href="{BASE}/c">', {"canonical-outside-head"}),
    "multiple-same": ('<link rel="canonical" href="/c"><link rel="canonical" href="/c">', "", {"multiple-canonicals"}),
    "multiple-different": ('<link rel="canonical" href="/a"><link rel="canonical" href="/b">', "",
                           {"multiple-canonicals", "canonicalized-to-other-url", "canonical-target-error"}),
    "cross-domain": ('<link rel="canonical" href="https://example.org/x">', "", {"canonical-cross-host"}),
    "fragment": ('<link rel="canonical" href="/c#top">', "", set()),
    "empty-href": ('<link rel="canonical" href="">', "", {"malformed-canonical"}),
    "no-href": ('<link rel="canonical">', "", {"malformed-canonical"}),
    "javascript": ('<link rel="canonical" href="javascript:void(0)">', "", {"malformed-canonical"}),
    "after-div-in-head": ('<div>x</div><link rel="canonical" href="/c">', "", {"canonical-outside-head"}),
}  # fmt: skip


@pytest.mark.parametrize("name", list(CANONICALS))
def test_canonical_matrix(name: str) -> None:
    head, body, expected = CANONICALS[name]
    with live_server() as srv:
        srv.page("/", html_page("Home page title", links=("/c",)))
        srv.page("/c", html_page("Canonical probe page", head=head, body=body))
        report = live_audit(srv)
    url = srv.base + "/c"
    found = {i.id for i in report.issues if url in i.affected_urls and "canonical" in i.id}
    assert found == expected


# --------------------------------------------------------------------------- #
# Crawl failure semantics: "could not retrieve" is never "empty site"
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _audit(url: str) -> object:
    from web_visibility.audit import run_audit

    return run_audit(url, CrawlConfig(fetch=FetchConfig(allow_private=True, delay=0, timeout=3)))


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(lambda s: s.page("/robots.txt", "down", status=503), id="robots-503"),
        pytest.param(lambda s: (s.page("/robots.txt", "", status=404), s.page("/", "err", status=500)), id="start-500"),
        pytest.param(lambda s: (s.page("/robots.txt", "", status=404),
                                s.page("/", "", status=429, headers={"Retry-After": "9999"})), id="start-429"),
        pytest.param(lambda s: (s.page("/robots.txt", "", status=404),
                                s.route("/", lambda h: send(h, 302, headers={"Location": "/a"})),
                                s.route("/a", lambda h: send(h, 302, headers={"Location": "/"}))), id="redirect-loop"),
        pytest.param(lambda s: (s.page("/robots.txt", "", status=404),
                                s.page("/", "hello", content_type="text/plain")), id="start-not-html"),
    ],
)  # fmt: skip
def test_failed_retrieval_is_an_incomplete_audit(setup) -> None:  # type: ignore[no-untyped-def]
    with live_server() as srv:
        setup(srv)
        report = live_audit(srv)
    assert report.crawl.state == "failed"
    assert report.score.overall is None and report.score.status == "not-scored"
    assert "start-url-unavailable" in {i.id for i in report.issues}


@pytest.mark.parametrize(
    "url",
    [f"http://127.0.0.1:{_free_port()}/", "http://no-such-host.invalid/"],
    ids=["refused", "dns"],
)
def test_unreachable_host_is_an_incomplete_audit(url: str) -> None:
    report = _audit(url)
    assert report.crawl.state == "failed"  # type: ignore[attr-defined]
    assert report.score.overall is None  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Retry-After / Crawl-delay (fake clock, real server) and N8
# --------------------------------------------------------------------------- #


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 2))
        self.now += seconds

    def clock(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("status", "retry_after", "requests", "slept"),
    [(429, "1", 2, 1.0), (429, "5", 2, 5.0), (503, "2", 2, 2.0), (429, "3600", 1, 0.0),
     (429, "-5", 1, 0.0), (429, None, 1, 0.0), (503, None, 1, 0.0)],
)  # fmt: skip
def test_retry_after_matrix(
    status: int, retry_after: str | None, requests: int, slept: float
) -> None:
    clock = FakeTime()
    with live_server() as srv:
        headers = {"Retry-After": retry_after} if retry_after else None
        srv.route("/x", lambda h: send(h, status, headers=headers))
        with Fetcher(
            FetchConfig(allow_private=True, delay=0), sleep=clock.sleep, clock=clock.clock
        ) as f:
            assert f.fetch(srv.base + "/x").status_code == status
        assert srv.hits("/x") == requests
    assert sum(clock.sleeps) == slept


def test_crawl_delay_above_the_limit_sends_only_robots_and_the_start_page() -> None:
    """N8: link checks (and sitemap fetches) used to continue at 30 s intervals."""
    clock = FakeTime()
    with live_server() as srv:
        srv.page("/robots.txt", "User-agent: *\nCrawl-delay: 100\nSitemap: {BASE}/s.xml\n",
                 content_type="text/plain")  # fmt: skip
        srv.page("/", html_page("Home page title", links=tuple(f"/p{i}" for i in range(10))))
        fetcher = Fetcher(
            FetchConfig(allow_private=True, delay=0.25), sleep=clock.sleep, clock=clock.clock
        )
        result = Crawler(CrawlConfig(), fetcher=fetcher).crawl(srv.base + "/")
        paths = [p for _, p, _ in srv.log]
    assert paths == ["/robots.txt", "/"]
    assert result.stop_reason == "crawl-delay-too-large"
    assert len(result.unchecked_links) == 10  # listed, not silently dropped
    assert not result.sitemaps.checked
    assert sum(clock.sleeps) < 31


def test_rate_limit_during_link_checks_stops_all_further_requests() -> None:
    with live_server() as srv:
        srv.page("/robots.txt", "User-agent: *\nAllow: /\n", content_type="text/plain")
        srv.page("/sitemap.xml", "", status=404)
        srv.page("/", html_page("Home page title", links=tuple(f"/p{i}" for i in range(10))))
        srv.default = lambda h: send(h, 429, b"slow down")
        report = live_audit(srv, max_pages=1)
        requests_after_first_429 = srv.hits()
    assert report.crawl.stop_reason == "rate-limited"
    assert requests_after_first_429 <= 4  # robots, sitemap, start page, one link check
    assert len(report.crawl.unchecked_links) >= 9
