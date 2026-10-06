"""Bounded, same-origin crawler.

Crawl sequence:

1. Fetch ``/robots.txt`` for the start URL's origin (one retry on a transient
   network failure). Its state decides whether crawling may proceed: a 5xx, 429
   or network failure means "assume complete disallow" (RFC 9309).
2. Fetch the start URL. If it redirects to the same host (http -> https, or
   adding/removing ``www.``), adopt that origin and its robots.txt.
3. Apply robots.txt ``Crawl-delay`` (up to ``max_crawl_delay``).
4. Discover sitemaps (robots.txt ``Sitemap:`` lines plus a guessed ``/sitemap.xml``),
   following sitemap indexes up to a file limit.
5. Breadth-first crawl over same-origin links, then sitemap URLs not reached
   through links (this is what makes orphan detection possible).
6. Verify link targets that were not crawled, within a request budget.

A 429 that is not resolved by one ``Retry-After`` retry stops the crawl: the
auditor backs off rather than continuing to hit a server that asked it to slow down.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace

from web_visibility.config import ROBOTS_USER_AGENT_TOKEN, CrawlConfig
from web_visibility.fetch import Fetcher
from web_visibility.models import (
    CrawlResult,
    DiscoverySource,
    FetchResult,
    Page,
    RobotsResult,
    SitemapFile,
    SitemapResult,
    SitemapSource,
    classify_robots_response,
)
from web_visibility.parsing import parse_html
from web_visibility.robots import MAX_ROBOTS_BYTES, RobotsTxt
from web_visibility.sitemap import MAX_SITEMAP_BYTES, SitemapParseError, parse_sitemap
from web_visibility.urls import (
    has_non_html_extension,
    normalize_url,
    origin,
    same_host,
    same_origin,
    same_site,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

__all__ = ["CrawlConfig", "CrawlResult", "Crawler", "InvalidStartURLError"]

_ROBOTS_TEXT_LIMIT = 20_000  # characters of robots.txt kept in the report
_HTML_SNIFF = (b"<!doctype html", b"<html")
_HOST_DOWN_ERRORS = frozenset({"connection", "dns", "ssl", "timeout"})


class InvalidStartURLError(ValueError):
    """The start URL is not an absolute http(s) URL."""


class Crawler:
    def __init__(
        self,
        config: CrawlConfig | None = None,
        fetcher: Fetcher | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.config = config or CrawlConfig()
        self._fetcher = fetcher or Fetcher(self.config.fetch)
        self._owns_fetcher = fetcher is None
        self._progress = progress

    def crawl(self, start_url: str) -> CrawlResult:
        start = normalize_url(start_url)
        if start is None:
            raise InvalidStartURLError(
                f"{start_url!r} is not an absolute http(s) URL (example: https://example.com)"
            )
        started = time.monotonic()
        requests_before = self._fetcher.request_count
        run = _CrawlRun(start)
        try:
            robots = self._fetch_robots(start)
            host_down = robots.error_kind in _HOST_DOWN_ERRORS
            first: FetchResult | None = None
            if not host_down and self._may_crawl(start, robots):
                first = self._fetcher.fetch(start, retry_transient=True)
                moved = normalize_url(first.final_url) if first.ok else None
                if moved and not same_origin(moved, start) and same_site(moved, start):
                    # The site lives at another origin of the same host: crawl there,
                    # under that origin's own robots.txt.
                    run.site = moved
                    robots = self._fetch_robots(moved)

            crawl_delay = self._apply_crawl_delay(robots, run)
            if host_down:
                sitemaps = SitemapResult(checked=False)
            else:
                sitemaps = self._fetch_sitemaps(run.site, robots)
            self._crawl_pages(run, robots, sitemaps, start, first)
            if run.stop_reason == "rate-limited":
                link_checks: dict[str, FetchResult] = {}
                unchecked: tuple[str, ...] = ()
            else:
                link_checks, unchecked = self._check_links(run, robots)
        finally:
            if self._owns_fetcher:
                self._fetcher.close()

        return CrawlResult(
            start_url=start,
            site_url=run.site,
            config=self.config,
            pages=tuple(run.pages),
            robots=robots,
            sitemaps=sitemaps,
            link_checks=link_checks,
            skipped=run.skipped,
            pending=tuple(url for url, _, _ in run.frontier),
            unchecked_links=unchecked,
            request_count=self._fetcher.request_count - requests_before,
            duration_seconds=round(time.monotonic() - started, 3),
            stop_reason=run.stop_reason,
            crawl_delay=crawl_delay,
        )

    # ------------------------------------------------------------------ #
    # robots.txt
    # ------------------------------------------------------------------ #

    def _fetch_robots(self, start: str) -> RobotsResult:
        url = f"{origin(start)}/robots.txt"
        self._report(f"Fetching {url}")
        result = self._fetcher.fetch(
            url, max_bytes=MAX_ROBOTS_BYTES, accept="text/plain,*/*;q=0.5", retry_transient=True
        )
        state = classify_robots_response(result.status_code, result.error_kind)
        if state != "parsed":
            return RobotsResult(
                url,
                state,
                status_code=result.status_code,
                content_type=result.content_type,
                error=result.error,
                error_kind=result.error_kind,
            )
        text = result.body.decode(result.charset or "utf-8", errors="replace").lstrip("\ufeff")
        head = text.lstrip()[:200].lower()
        return RobotsResult(
            url,
            state,
            status_code=result.status_code,
            content_type=result.content_type,
            text=text[:_ROBOTS_TEXT_LIMIT],
            rules=RobotsTxt.parse(text),
            looks_like_html=result.content_type == "text/html"
            or head.startswith(("<!doctype html", "<html")),
            truncated=result.truncated,  # RFC 9309: parsing the first 500 KiB is enough
        )

    def _may_crawl(self, url: str, robots: RobotsResult) -> bool:
        if self.config.respect_robots and not robots.allows(url, ROBOTS_USER_AGENT_TOKEN):
            return False
        return not has_non_html_extension(url)

    def _apply_crawl_delay(self, robots: RobotsResult, run: _CrawlRun) -> float | None:
        if not self.config.respect_robots or robots.rules is None:
            return None
        delay = robots.rules.crawl_delay(ROBOTS_USER_AGENT_TOKEN)
        if delay is None:
            return None
        if delay > self.config.max_crawl_delay:
            # Honouring it would make a multi-page audit take too long; fetch only
            # the start page rather than ignore the site's request.
            run.stop_reason = "crawl-delay-too-large"
            run.page_budget = 1
        self._fetcher.set_min_delay(min(delay, self.config.max_crawl_delay))
        return delay

    # ------------------------------------------------------------------ #
    # Sitemaps
    # ------------------------------------------------------------------ #

    def _fetch_sitemaps(self, site: str, robots: RobotsResult) -> SitemapResult:
        queue: deque[tuple[str, SitemapSource]] = deque()
        seen: set[str] = set()
        files: list[SitemapFile] = []

        declared = robots.rules.sitemaps if robots.rules else ()
        for raw in declared:
            url = normalize_url(raw)
            if url is None:
                error = "not an absolute http(s) URL"
                files.append(SitemapFile(raw, "robots", error=error, error_kind="invalid-url"))
            elif url not in seen:
                seen.add(url)
                queue.append((url, "robots"))
        default = f"{origin(site)}/sitemap.xml"
        if default not in seen:
            seen.add(default)
            queue.append((default, "default"))

        urls: dict[str, None] = {}
        url_limit_reached = False
        fetched = 0
        while queue and fetched < self.config.max_sitemaps:
            url, source = queue.popleft()
            fetched += 1
            self._report(f"Fetching sitemap {url}")
            file, locs = self._fetch_sitemap(url, source)
            files.append(file)
            if file.kind == "sitemapindex":
                for child in file.children:
                    if child not in seen:
                        seen.add(child)
                        queue.append((child, "index"))
                continue
            for loc in locs:
                if len(urls) >= self.config.max_sitemap_urls:
                    url_limit_reached = True
                    break
                urls.setdefault(loc, None)

        return SitemapResult(
            files=tuple(files),
            urls=tuple(urls),
            url_limit_reached=url_limit_reached,
            file_limit_reached=bool(queue),
        )

    def _fetch_sitemap(self, url: str, source: SitemapSource) -> tuple[SitemapFile, list[str]]:
        result = self._fetcher.fetch(
            url,
            max_bytes=MAX_SITEMAP_BYTES,
            accept="application/xml,text/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
        )
        base = SitemapFile(
            url,
            source,
            status_code=result.status_code,
            error=result.error,
            error_kind=result.error_kind,
        )
        if not base.fetched:
            return base, []
        if result.truncated:
            return replace(base, parse_error=f"sitemap larger than {MAX_SITEMAP_BYTES} bytes"), []
        try:
            document = parse_sitemap(result.body, result.content_type)
        except SitemapParseError as exc:
            return replace(base, parse_error=str(exc), looks_like_html=exc.looks_like_html), []

        valid: list[str] = []
        invalid: list[str] = []
        duplicates: dict[str, None] = {}
        cross_host: list[str] = []
        seen: set[str] = set()
        for loc in document.locs:
            normalized = normalize_url(loc) if loc and not any(c.isspace() for c in loc) else None
            if normalized is None:
                invalid.append(loc)
                continue
            if normalized in seen:
                duplicates.setdefault(normalized, None)
                continue
            seen.add(normalized)
            valid.append(normalized)
            if not same_host(normalized, url):
                cross_host.append(normalized)

        is_index = document.kind == "sitemapindex"
        file = replace(
            base,
            kind=document.kind,
            url_count=len(valid),
            invalid_urls=tuple(invalid),
            duplicate_urls=tuple(duplicates),
            cross_host_urls=() if is_index else tuple(cross_host),
            children=tuple(valid) if is_index else (),
        )
        return file, ([] if is_index else valid)

    # ------------------------------------------------------------------ #
    # Pages
    # ------------------------------------------------------------------ #

    def _crawl_pages(
        self,
        run: _CrawlRun,
        robots: RobotsResult,
        sitemaps: SitemapResult,
        start: str,
        first: FetchResult | None,
    ) -> None:
        """Breadth-first crawl. ``first`` is the already-fetched start URL, if any."""
        run.enqueue(start, 0, DiscoverySource.START)
        budget = min(self.config.max_pages, run.page_budget)
        sitemap_seeded = False
        while len(run.pages) < budget:
            if not run.frontier:
                if sitemap_seeded:
                    break
                sitemap_seeded = True
                self._seed_sitemap(run, sitemaps)
                continue

            url, depth, via = run.frontier.popleft()
            if self.config.respect_robots and not robots.allows(url, ROBOTS_USER_AGENT_TOKEN):
                run.skipped[url] = robots.skip_reason
                continue
            if has_non_html_extension(url):
                run.skipped[url] = "non-html-resource"
                continue

            self._report(f"Crawling [{len(run.pages) + 1}/{budget}] {url}")
            prefetched = first if (first is not None and url == start) else None
            page = self._fetch_page(run.site, url, depth, via, prefetched)
            run.pages.append(page)
            run.mark_seen(page.final_url)
            if page.status_code == 429:
                run.stop_reason = "rate-limited"
                break
            if page.content is None:
                continue
            for link in page.content.links:
                if same_origin(link.url, run.site):
                    run.enqueue(link.url, depth + 1, DiscoverySource.LINK)

        if not sitemap_seeded:  # stopped first: unseeded sitemap URLs are pending too
            self._seed_sitemap(run, sitemaps)

    def _seed_sitemap(self, run: _CrawlRun, sitemaps: SitemapResult) -> None:
        for url in sitemaps.urls:
            if same_origin(url, run.site):
                run.enqueue(url, 1, DiscoverySource.SITEMAP)

    def _fetch_page(
        self,
        site: str,
        url: str,
        depth: int,
        via: DiscoverySource,
        prefetched: FetchResult | None = None,
    ) -> Page:
        result = prefetched or self._fetcher.fetch(url)
        final = normalize_url(result.final_url) or result.final_url
        page = Page(
            requested_url=url,
            final_url=final,
            status_code=result.status_code,
            content_type=result.content_type,
            response_headers=result.headers,
            discovered_via=via,
            depth=depth,
            redirect_chain=result.redirect_chain,
            error=result.error,
            error_kind=result.error_kind,
            truncated=result.truncated,
        )
        if not (page.is_success and same_origin(final, site) and _is_html(result)):
            return page
        if result.truncated:
            # An incomplete document would produce false "missing" findings.
            limit = self._fetcher.config.max_bytes
            message = f"response exceeded {limit} bytes (decompressed) and was not analyzed"
            return replace(page, error=message, error_kind="too-large")
        try:
            content = parse_html(result.body, final, encoding=result.charset)
        except Exception as exc:  # malformed input must never abort the crawl
            logger.warning("could not parse %s: %s", final, exc)
            return replace(page, error=f"HTML could not be parsed: {exc}", error_kind="parse")
        return replace(page, content=content, content_type=page.content_type or "text/html")

    # ------------------------------------------------------------------ #
    # Link checks
    # ------------------------------------------------------------------ #

    def _check_links(
        self, run: _CrawlRun, robots: RobotsResult
    ) -> tuple[dict[str, FetchResult], tuple[str, ...]]:
        crawled = {p.requested_url for p in run.pages} | {p.final_url for p in run.pages}
        internal: dict[str, None] = {}
        external: dict[str, None] = {}
        for page in run.pages:
            if page.content is None:
                continue
            targets = [link.url for link in page.content.links]
            targets += [
                c
                for link in page.canonical_candidates
                if link.href and (c := normalize_url(link.href, page.final_url))
            ]
            for target in targets:
                # Skipped non-HTML resources (PDFs, images...) are still link-checked.
                if target in crawled or run.skipped.get(target, "").startswith("robots"):
                    continue
                (internal if same_host(target, run.site) else external)[target] = None

        checks: dict[str, FetchResult] = {}
        unchecked: list[str] = []
        budget = self.config.max_link_checks
        for target in internal:
            if self.config.respect_robots and not robots.allows(target, ROBOTS_USER_AGENT_TOKEN):
                run.skipped[target] = robots.skip_reason
                continue
            if budget <= 0 or run.stop_reason == "rate-limited":
                unchecked.append(target)
                continue
            budget -= 1
            checks[target] = self._check(target)
            if checks[target].status_code == 429:
                run.stop_reason = "rate-limited"

        if self.config.check_external:
            budget = self.config.max_external_checks
            for target in external:
                if budget <= 0:
                    unchecked.append(target)
                    continue
                budget -= 1
                checks[target] = self._check(target)
        return checks, tuple(unchecked)

    def _check(self, url: str) -> FetchResult:
        self._report(f"Checking link {url}")
        result = self._fetcher.fetch(url, method="HEAD", read_body=False)
        failed = result.status_code is None or result.status_code >= 400
        retry = failed and result.error_kind not in ("blocked", "dns") and result.status_code != 429
        if retry:  # many servers mishandle HEAD; confirm with GET before reporting
            result = self._fetcher.fetch(url, method="GET", read_body=False)
        return result

    def _report(self, message: str) -> None:
        logger.debug(message)
        if self._progress is not None:
            self._progress(message)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _CrawlRun:
    """Mutable bookkeeping for a single crawl (never shared between crawls)."""

    def __init__(self, start: str) -> None:
        self.site = start
        self.frontier: deque[tuple[str, int, DiscoverySource]] = deque()
        self.pages: list[Page] = []
        self.skipped: dict[str, str] = {}
        self.stop_reason: str | None = None
        self.page_budget = 1_000_000
        self._seen: set[str] = set()

    def enqueue(self, url: str, depth: int, via: DiscoverySource) -> None:
        if url not in self._seen:
            self._seen.add(url)
            self.frontier.append((url, depth, via))

    def mark_seen(self, url: str) -> None:
        self._seen.add(url)


def _is_html(result: FetchResult) -> bool:
    if result.content_type in ("text/html", "application/xhtml+xml"):
        return True
    if result.content_type is None:  # missing header: sniff the start of the body
        return result.body.lstrip()[:64].lower().startswith(_HTML_SNIFF)
    return False
