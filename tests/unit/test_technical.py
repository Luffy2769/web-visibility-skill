from dataclasses import replace

from web_visibility.analyzers import crawlability, technical
from web_visibility.models import DiscoverySource, RedirectHop, Severity, SitemapFile, SitemapResult

from factories import BASE, by_id, good_head, html_doc, ids, make_context, make_page, make_robots


def home(extra_head: str = "", body: str = '<a href="/a">A</a>', **kwargs):  # type: ignore[no-untyped-def]
    return make_page("/", html_doc(good_head("/") + extra_head, body), **kwargs)


def sitemap(
    *paths: str, duplicate_urls: tuple[str, ...] = (), invalid_urls: tuple[str, ...] = ()
) -> SitemapResult:
    urls = tuple(f"{BASE}{p}" for p in paths)
    file = SitemapFile(
        f"{BASE}/sitemap.xml",
        "robots",
        status_code=200,
        kind="urlset",
        url_count=len(urls),
        duplicate_urls=duplicate_urls,
        invalid_urls=invalid_urls,
    )
    return SitemapResult(files=(file,), urls=urls)


# --- technical --------------------------------------------------------------------


def test_healthy_https_page_has_no_technical_issues() -> None:
    assert technical.analyze(make_context([home()])) == []


def test_start_url_unavailable_variants() -> None:
    down = technical.analyze(
        make_context([make_page("/", error="connection refused", error_kind="connection")])
    )
    assert by_id(down, "start-url-unavailable")[0].severity is Severity.CRITICAL
    not_found = technical.analyze(make_context([make_page("/", "<html></html>", status=404)]))
    assert "HTTP 404" in by_id(not_found, "start-url-unavailable")[0].evidence


def test_start_url_skipped_by_robots_is_explained() -> None:
    ctx = make_context([], skipped={f"{BASE}/": "robots-disallowed"})
    issue = by_id(technical.analyze(ctx), "start-url-unavailable")[0]
    assert "--ignore-robots" in issue.description


def test_plain_http_site() -> None:
    page = make_page("/", html_doc(good_head("/")))
    http_page = replace(page, requested_url="http://example.com/", final_url="http://example.com/")
    ctx = make_context([http_page], start_url="http://example.com/")
    issue = by_id(technical.analyze(ctx), "https-not-used")[0]
    assert issue.severity is Severity.HIGH
    assert issue.prevalence == 1.0


def test_plain_http_on_localhost_is_informational() -> None:
    page = make_page("/", html_doc(good_head("/")))
    local = replace(
        page, requested_url="http://127.0.0.1:8000/", final_url="http://127.0.0.1:8000/"
    )
    ctx = make_context([local], start_url="http://127.0.0.1:8000/")
    assert by_id(technical.analyze(ctx), "https-not-used")[0].severity is Severity.INFO


def test_mixed_content() -> None:
    page = home(extra_head='<script src="http://cdn.example.org/lib.js"></script>')
    assert by_id(technical.analyze(make_context([page])), "mixed-content")[0].details[
        "resources"
    ] == ["http://cdn.example.org/lib.js"]


def test_redirect_chain() -> None:
    page = make_page("/a", html_doc(good_head("/a")), redirect_from="/old")
    chained = replace(
        page, redirect_chain=(RedirectHop(f"{BASE}/old", 301), RedirectHop(f"{BASE}/mid", 302))
    )
    assert "redirect-chain" in ids(technical.analyze(make_context([home(), chained])))


def test_noindex_signals() -> None:
    noindex = '<meta name="robots" content="noindex">'
    start_issues = technical.analyze(make_context([home(extra_head=noindex)]))
    assert by_id(start_issues, "noindex-start-page")[0].severity is Severity.HIGH
    other = make_page("/a", html_doc(good_head("/a")), headers={"x-robots-tag": "noindex"})
    assert (
        by_id(technical.analyze(make_context([home(), other])), "noindex-page")[0].severity
        is Severity.INFO
    )


def test_agent_scoped_x_robots_tag_is_not_treated_as_global() -> None:
    page = make_page(
        "/a", html_doc(good_head("/a")), headers={"x-robots-tag": "googlebot: noindex, nofollow"}
    )
    assert not page.is_noindex
    assert "nofollow" not in page.robots_directives


def test_crawl_limit_note() -> None:
    ctx = make_context([home()], pending=(f"{BASE}/x",))
    assert by_id(technical.analyze(ctx), "crawl-limit-reached")[0].severity is Severity.INFO


# --- robots.txt -------------------------------------------------------------------


def test_robots_missing_is_low() -> None:
    issues = crawlability.analyze(make_context([home()], robots=make_robots(None, status=404)))
    assert by_id(issues, "robots-txt-missing")[0].severity is Severity.LOW


def test_robots_server_error_is_high() -> None:
    issues = crawlability.analyze(make_context([home()], robots=make_robots(None, status=503)))
    assert by_id(issues, "robots-txt-unreachable")[0].severity is Severity.HIGH


def test_robots_access_denied() -> None:
    issues = crawlability.analyze(make_context([home()], robots=make_robots(None, status=403)))
    assert "robots-txt-access-denied" in ids(issues)


def test_disallow_all_is_critical_and_worded_conservatively() -> None:
    issues = crawlability.analyze(
        make_context([home()], robots=make_robots("User-agent: *\nDisallow: /\n"))
    )
    issue = by_id(issues, "robots-disallow-all")[0]
    assert issue.severity is Severity.CRITICAL
    assert "appears" in issue.title
    assert "will never" not in issue.description.lower()


def test_crawled_pages_disallowed_when_robots_ignored() -> None:
    robots = make_robots("User-agent: *\nDisallow: /a\n")
    pages = [home(), make_page("/a", html_doc(good_head("/a")))]
    issue = by_id(
        crawlability.analyze(make_context(pages, robots=robots)), "crawled-page-disallowed"
    )[0]
    assert issue.affected_urls == (f"{BASE}/a",)


def test_robots_served_as_html() -> None:
    robots = make_robots("<!DOCTYPE html><html>Not found</html>")
    robots = replace(robots, looks_like_html=True)
    assert "robots-txt-not-plain-text" in ids(
        crawlability.analyze(make_context([home()], robots=robots))
    )


# --- sitemaps ---------------------------------------------------------------------


def test_no_sitemap_found() -> None:
    default_404 = SitemapResult(
        files=(SitemapFile(f"{BASE}/sitemap.xml", "default", status_code=404),)
    )
    issues = crawlability.analyze(make_context([home()], sitemaps=default_404))
    assert ids(issues) == ["sitemap-missing"]  # the 404 guess itself is not reported


def test_referenced_sitemap_inaccessible_and_invalid() -> None:
    files = (
        SitemapFile(f"{BASE}/a.xml", "robots", status_code=500),
        SitemapFile(f"{BASE}/b.xml", "robots", status_code=200, parse_error="invalid XML: x"),
    )
    issues = crawlability.analyze(make_context([home()], sitemaps=SitemapResult(files=files)))
    assert {"sitemap-inaccessible", "sitemap-invalid", "sitemap-missing"} <= set(ids(issues))


def test_sitemap_content_issues_use_entry_prevalence() -> None:
    result = sitemap("/", "/a", "/b", "/c", duplicate_urls=(f"{BASE}/a",), invalid_urls=("nope",))
    issues = crawlability.analyze(make_context([home()], sitemaps=result))
    dup = by_id(issues, "sitemap-duplicate-urls")[0]
    assert dup.prevalence == 0.2  # 1 of 5 entries
    assert "sitemap-invalid-urls" in ids(issues)


def test_sitemap_vs_crawl_comparison() -> None:
    noindex = '<meta name="robots" content="noindex">'
    pages = [
        home(),
        make_page("/gone", status=404, via=DiscoverySource.SITEMAP),
        make_page(
            "/new", html_doc(good_head("/new")), redirect_from="/old", via=DiscoverySource.SITEMAP
        ),
        make_page("/hidden", html_doc(good_head("/hidden") + noindex), via=DiscoverySource.SITEMAP),
        make_page("/alt", html_doc(good_head("/main")), via=DiscoverySource.SITEMAP),
        make_page("/unlisted", html_doc(good_head("/unlisted"))),
    ]
    issues = crawlability.analyze(
        make_context(pages, sitemaps=sitemap("/", "/gone", "/old", "/hidden", "/alt"))
    )
    found = {i.id: i for i in issues}
    assert found["sitemap-url-error"].affected_urls == (f"{BASE}/gone",)
    assert found["sitemap-url-redirects"].affected_urls == (f"{BASE}/old",)
    assert found["sitemap-url-noindex"].affected_urls == (f"{BASE}/hidden",)
    assert found["sitemap-url-non-canonical"].affected_urls == (f"{BASE}/alt",)
    assert f"{BASE}/unlisted" in found["page-not-in-sitemap"].affected_urls


def test_sitemap_urls_disallowed_by_robots() -> None:
    issues = crawlability.analyze(
        make_context(
            [home()],
            robots=make_robots("User-agent: *\nDisallow: /private\n"),
            sitemaps=sitemap("/", "/private/x"),
        )
    )
    issue = by_id(issues, "sitemap-url-disallowed")[0]
    assert issue.prevalence == 0.5


def test_sitemap_not_referenced_in_robots() -> None:
    default = SitemapResult(
        files=(
            SitemapFile(
                f"{BASE}/sitemap.xml", "default", status_code=200, kind="urlset", url_count=1
            ),
        ),
        urls=(f"{BASE}/",),
    )
    assert "sitemap-not-in-robots" in ids(
        crawlability.analyze(make_context([home()], sitemaps=default))
    )
