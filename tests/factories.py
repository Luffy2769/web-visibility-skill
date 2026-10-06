"""Builders for unit tests: pages, crawl results and audit contexts without a network."""

from __future__ import annotations

from collections.abc import Iterable

from web_visibility.analyzers import AuditContext
from web_visibility.audit import AuditReport, build_report
from web_visibility.config import CrawlConfig, FetchConfig
from web_visibility.models import (
    CrawlResult,
    DiscoverySource,
    FetchResult,
    Issue,
    Page,
    RedirectHop,
    RobotsResult,
    SitemapResult,
    classify_robots_response,
)
from web_visibility.parsing import parse_html
from web_visibility.robots import RobotsTxt

BASE = "https://example.com"


def html_doc(head: str = "", body: str = "") -> str:
    return f"<!DOCTYPE html><html lang='en'><head>{head}</head><body>{body}</body></html>"


def good_head(path: str = "/", title: str | None = None) -> str:
    """A <head> that passes every metadata check."""
    title = title or f"A descriptive page title for {path}"
    return (
        f"<title>{title}</title>"
        f'<meta name="description" content="Description of {path}">'
        f'<link rel="canonical" href="{BASE}{path}">'
    )


def make_page(
    path: str = "/",
    html: str | None = None,
    *,
    status: int = 200,
    via: DiscoverySource | None = None,
    headers: dict[str, str] | None = None,
    redirect_from: str | None = None,
    error: str | None = None,
    error_kind: str | None = None,
) -> Page:
    url = f"{BASE}{path}"
    content = None
    if html is not None and 200 <= status < 300 and error is None:
        content = parse_html(html.encode(), url)
    chain = (RedirectHop(f"{BASE}{redirect_from}", 301),) if redirect_from else ()
    return Page(
        requested_url=f"{BASE}{redirect_from}" if redirect_from else url,
        final_url=url,
        status_code=None if error else status,
        content_type="text/html" if html is not None else None,
        response_headers=headers or {},
        discovered_via=via or (DiscoverySource.START if path == "/" else DiscoverySource.LINK),
        depth=0 if path == "/" else 1,
        redirect_chain=chain,
        error=error,
        error_kind=error_kind,
        content=content,
    )


def make_robots(
    text: str | None = "User-agent: *\nDisallow:\n",
    status: int | None = 200,
    *,
    error_kind: str | None = None,
) -> RobotsResult:
    url = f"{BASE}/robots.txt"
    state = classify_robots_response(status, error_kind)
    if state != "parsed" or text is None:
        if state == "parsed":
            state = "missing"
        error = "failed" if error_kind else None
        return RobotsResult(url, state, status_code=status, error=error, error_kind=error_kind)
    return RobotsResult(
        url,
        state,
        status_code=status,
        content_type="text/plain",
        text=text,
        rules=RobotsTxt.parse(text),
    )


def make_crawl(
    pages: Iterable[Page],
    *,
    robots: RobotsResult | None = None,
    sitemaps: SitemapResult | None = None,
    link_checks: dict[str, FetchResult] | None = None,
    skipped: dict[str, str] | None = None,
    pending: tuple[str, ...] = (),
    unchecked: tuple[str, ...] = (),
    config: CrawlConfig | None = None,
    start_url: str = f"{BASE}/",
    stop_reason: str | None = None,
) -> CrawlResult:
    return CrawlResult(
        start_url=start_url,
        site_url=start_url,
        config=config or CrawlConfig(fetch=FetchConfig(delay=0)),
        pages=tuple(pages),
        robots=robots or make_robots(),
        sitemaps=sitemaps or SitemapResult(),
        link_checks=link_checks or {},
        skipped=skipped or {},
        pending=pending,
        unchecked_links=unchecked,
        request_count=0,
        duration_seconds=0.0,
        stop_reason=stop_reason,
    )


def make_context(pages: Iterable[Page], **kwargs: object) -> AuditContext:
    return AuditContext.build(make_crawl(pages, **kwargs))  # type: ignore[arg-type]


def make_report(pages: Iterable[Page], **kwargs: object) -> AuditReport:
    """Run the real analyzers, coverage and scoring over a synthetic crawl."""
    return build_report(f"{BASE}/", make_crawl(pages, **kwargs))  # type: ignore[arg-type]


def link_check(url: str, status: int | None, *, error_kind: str | None = None) -> FetchResult:
    return FetchResult(
        requested_url=url,
        final_url=url,
        status_code=status,
        headers={},
        error="failed" if error_kind else None,
        error_kind=error_kind,
    )


def ids(issues: Iterable[Issue]) -> list[str]:
    return [issue.id for issue in issues]


def by_id(issues: Iterable[Issue], rule_id: str) -> list[Issue]:
    return [issue for issue in issues if issue.id == rule_id]
