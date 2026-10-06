"""Crawler behaviour against an in-memory site (httpx.MockTransport)."""

from __future__ import annotations

import httpx
import pytest

from web_visibility.crawler import CrawlConfig, Crawler, CrawlResult, InvalidStartURLError
from web_visibility.fetch import FetchConfig, Fetcher

HTML = "text/html; charset=utf-8"
ROBOTS_BLOCKED = "User-agent: *\nDisallow: /blocked\n"


def page(*links: str, title: str = "Page") -> str:
    anchors = "".join(f'<a href="{href}">link</a>' for href in links)
    return f"<html><head><title>{title}</title></head><body>{anchors}</body></html>"


class FakeSite:
    """Routes: path -> (status, content-type, body) or redirect target."""

    def __init__(self, routes: dict[str, tuple[int, str, str]]) -> None:
        self.routes = routes
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url}")
        key = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        route = (
            self.routes.get(f"{request.url.host}{request.url.path}")  # host-specific route
            or self.routes.get(key)
            or self.routes.get(request.url.path)
        )
        if route is None:
            return httpx.Response(404, headers={"Content-Type": HTML}, text="not found")
        status, content_type, body = route
        if 300 <= status < 400:
            return httpx.Response(status, headers={"Location": body})
        return httpx.Response(status, headers={"Content-Type": content_type}, text=body)


def crawl(site: FakeSite, start: str = "https://example.com/", **config: object) -> CrawlResult:
    client = httpx.Client(transport=httpx.MockTransport(site), follow_redirects=False)
    fetch = FetchConfig(delay=0, allow_private=True)
    fetcher = Fetcher(fetch, client=client)
    crawler = Crawler(CrawlConfig(fetch=fetch, **config), fetcher=fetcher)  # type: ignore[arg-type]
    return crawler.crawl(start)


def crawled_paths(result: CrawlResult) -> list[str]:
    return [p.requested_url.removeprefix("https://example.com") for p in result.pages]


def test_follows_internal_links_breadth_first() -> None:
    site = FakeSite(
        {
            "/": (200, HTML, page("/a", "/b")),
            "/a": (200, HTML, page("/a/deep")),
            "/b": (200, HTML, page("/")),
            "/a/deep": (200, HTML, page()),
        }
    )
    result = crawl(site)
    assert crawled_paths(result) == ["/", "/a", "/b", "/a/deep"]
    assert [p.depth for p in result.pages] == [0, 1, 1, 2]
    assert not result.limit_reached


def test_page_limit_is_enforced_and_pending_recorded() -> None:
    links = [f"/p{i}" for i in range(10)]
    site = FakeSite({"/": (200, HTML, page(*links)), **{p: (200, HTML, page()) for p in links}})
    result = crawl(site, max_pages=3)
    assert len(result.pages) == 3
    assert result.limit_reached
    assert len(result.pending) == 8


def test_same_origin_only_and_external_links_not_fetched() -> None:
    site = FakeSite(
        {
            "/": (
                200,
                HTML,
                page(
                    "https://other.example/",
                    "http://example.com/insecure",
                    "https://sub.example.com/",
                ),
            ),
        }
    )
    result = crawl(site)
    assert crawled_paths(result) == ["/"]
    page_requests = [r for r in site.requests if "robots" not in r and "sitemap" not in r]
    assert all("other.example" not in r and "sub.example.com" not in r for r in page_requests)


def test_fragments_tracking_params_and_duplicates_are_collapsed() -> None:
    site = FakeSite(
        {
            "/": (200, HTML, page("/a", "/a#top", "/a#", "/a?utm_source=x", "a", "/./a")),
            "/a": (200, HTML, page()),
        }
    )
    result = crawl(site)
    assert crawled_paths(result) == ["/", "/a"]


def test_robots_disallow_is_respected_by_default() -> None:
    site = FakeSite(
        {
            "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /private\n"),
            "/": (200, HTML, page("/private/x", "/public")),
            "/public": (200, HTML, page()),
            "/private/x": (200, HTML, page()),
        }
    )
    result = crawl(site)
    assert crawled_paths(result) == ["/", "/public"]
    assert result.skipped == {"https://example.com/private/x": "robots-disallowed"}

    ignored = crawl(site, respect_robots=False)
    assert "/private/x" in crawled_paths(ignored)


def test_unreachable_robots_means_full_disallow() -> None:
    site = FakeSite({"/robots.txt": (503, "text/plain", "busy"), "/": (200, HTML, page())})
    result = crawl(site)
    assert result.pages == ()
    assert result.skipped["https://example.com/"] == "robots-unavailable"
    assert result.robots.restricts_crawling


def test_missing_robots_allows_everything() -> None:
    site = FakeSite({"/": (200, HTML, page())})
    result = crawl(site)
    assert result.robots.status_code == 404 and not result.robots.exists
    assert crawled_paths(result) == ["/"]


def test_sitemap_urls_are_seeded_after_link_discovery() -> None:
    sitemap = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://example.com/a</loc></url>"
        "<url><loc>https://example.com/orphan</loc></url>"
        "<url><loc>https://elsewhere.example/x</loc></url>"
        "</urlset>"
    )
    site = FakeSite(
        {
            "/sitemap.xml": (200, "application/xml", sitemap),
            "/": (200, HTML, page("/a")),
            "/a": (200, HTML, page("/")),
            "/orphan": (200, HTML, page("/")),
        }
    )
    result = crawl(site)
    assert crawled_paths(result) == ["/", "/a", "/orphan"]
    assert result.pages[2].discovered_via == "sitemap"
    assert result.sitemaps.found
    assert "https://elsewhere.example/x" in result.sitemaps.files[0].cross_host_urls


def test_sitemap_index_and_robots_sitemap_reference() -> None:
    index = (
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://example.com/pages.xml</loc></sitemap></sitemapindex>"
    )
    pages_xml = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://example.com/</loc></url></urlset>"
    )
    site = FakeSite(
        {
            "/robots.txt": (200, "text/plain", "Sitemap: https://example.com/index.xml\n"),
            "/index.xml": (200, "application/xml", index),
            "/pages.xml": (200, "application/xml", pages_xml),
            "/": (200, HTML, page()),
        }
    )
    result = crawl(site)
    sources = {f.url.rsplit("/", 1)[1]: f.source for f in result.sitemaps.files}
    assert sources == {"index.xml": "robots", "sitemap.xml": "default", "pages.xml": "index"}
    assert result.sitemaps.urls == ("https://example.com/",)


def test_non_html_targets_are_not_parsed_as_pages() -> None:
    site = FakeSite(
        {
            "/": (200, HTML, page("/file.pdf", "/data")),
            "/data": (200, "application/json", '{"a": 1}'),
        }
    )
    result = crawl(site)
    assert result.skipped["https://example.com/file.pdf"] == "non-html-resource"
    data = next(p for p in result.pages if p.requested_url.endswith("/data"))
    assert data.content is None and data.status_code == 200


def test_uncrawled_internal_link_targets_are_checked_with_head_then_get() -> None:
    links = [f"/p{i}" for i in range(4)]
    site = FakeSite(
        {"/": (200, HTML, page(*links, "/gone")), **{p: (200, HTML, page()) for p in links}}
    )
    result = crawl(site, max_pages=2, max_link_checks=10)
    gone = result.link_checks["https://example.com/gone"]
    assert gone.status_code == 404
    assert "HEAD https://example.com/gone" in site.requests
    assert "GET https://example.com/gone" in site.requests  # confirmed before reporting


def test_link_check_budget() -> None:
    links = [f"/p{i}" for i in range(6)]
    site = FakeSite({"/": (200, HTML, page(*links))})
    result = crawl(site, max_pages=1, max_link_checks=2)
    assert len(result.link_checks) == 2
    assert len(result.unchecked_links) == 4


def test_external_links_checked_only_when_enabled() -> None:
    site = FakeSite({"/": (200, HTML, page("https://other.example/"))})
    assert crawl(site).link_checks == {}
    checked = crawl(site, check_external=True).link_checks
    assert "https://other.example/" in checked


def test_redirected_start_url() -> None:
    site = FakeSite({"/": (301, "", "https://example.com/home"), "/home": (200, HTML, page("/"))})
    result = crawl(site)
    start = result.pages[0]
    assert start.final_url == "https://example.com/home"
    assert start.content is not None
    assert crawled_paths(result) == ["/"]  # "/" is not crawled twice


def test_invalid_start_url() -> None:
    with pytest.raises(InvalidStartURLError):
        crawl(FakeSite({}), start="example.com")
    with pytest.raises(InvalidStartURLError):
        crawl(FakeSite({}), start="ftp://example.com/")


def test_crawl_config_validation() -> None:
    with pytest.raises(ValueError):
        CrawlConfig(max_pages=0)


def test_same_site_redirect_of_start_url_becomes_the_crawl_origin() -> None:
    site = FakeSite(
        {
            "example.com/": (301, "", "https://www.example.com/"),
            "www.example.com/robots.txt": (200, "text/plain", ROBOTS_BLOCKED),
            "www.example.com/": (200, HTML, page("/a", "/blocked", "https://www.example.com/b")),
            "www.example.com/a": (200, HTML, page("/")),
            "www.example.com/b": (200, HTML, page("/")),
        }
    )
    result = crawl(site, start="https://example.com/")
    assert result.start_url == "https://example.com/"
    assert result.site_url == "https://www.example.com/"
    assert [p.final_url for p in result.pages] == [
        "https://www.example.com/",
        "https://www.example.com/a",
        "https://www.example.com/b",
    ]
    assert result.pages[0].content is not None  # the start page is audited, not "off-site"
    assert "https://www.example.com/blocked" in result.skipped  # new origin's robots.txt applies
    assert sum("GET https://example.com/ " in f"{r} " for r in site.requests) == 1  # fetched once


def test_redirect_to_a_different_site_is_not_adopted() -> None:
    site = FakeSite(
        {
            "example.com/": (302, "", "https://unrelated.example.org/landing"),
            "unrelated.example.org/landing": (200, HTML, page("/x")),
        }
    )
    result = crawl(site, start="https://example.com/")
    assert result.site_url == "https://example.com/"
    assert result.pages[0].content is None
    assert len(result.pages) == 1


def test_linked_non_html_files_are_link_checked() -> None:
    site = FakeSite({"/": (200, HTML, page("/brochure.pdf"))})
    result = crawl(site)
    assert result.skipped["https://example.com/brochure.pdf"] == "non-html-resource"
    assert result.link_checks["https://example.com/brochure.pdf"].status_code == 404
