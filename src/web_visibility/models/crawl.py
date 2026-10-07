"""The outcome of a crawl."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from web_visibility.config import CrawlConfig
from web_visibility.models.page import FetchResult, Page, is_definitive_response
from web_visibility.models.robots import RobotsResult
from web_visibility.models.sitemap import SitemapResult

CrawlState = Literal["complete", "partial", "failed"]


@dataclass(frozen=True, slots=True)
class CrawlResult:
    start_url: str
    """The normalized URL the user asked for."""
    site_url: str
    """Where the crawl actually ran: ``start_url``, or its same-host redirect target
    (``http://example.com/`` -> ``https://www.example.com/``)."""
    config: CrawlConfig
    pages: tuple[Page, ...]
    robots: RobotsResult
    sitemaps: SitemapResult
    link_checks: dict[str, FetchResult]
    skipped: dict[str, str]
    """URL -> reason it was deliberately not fetched (e.g. ``robots-disallowed``)."""
    pending: tuple[str, ...]
    """Discovered same-origin URLs left uncrawled (page limit or early stop)."""
    unchecked_links: tuple[str, ...]
    """Link targets not verified because the link-check budget ran out."""
    request_count: int
    duration_seconds: float
    stop_reason: str | None = None
    """Why the crawl stopped early, e.g. ``rate-limited`` or ``crawl-delay-too-large``."""
    crawl_delay: float | None = None
    """robots.txt ``Crawl-delay`` that was applied (seconds), if any."""

    @property
    def limit_reached(self) -> bool:
        return bool(self.pending)

    @property
    def unretrieved_pages(self) -> tuple[Page, ...]:
        """Crawled pages whose content the auditor could not observe (see ``Page.unretrieved``)."""
        return tuple(p for p in self.pages if p.unretrieved)

    @property
    def audited_page_count(self) -> int:
        return len({p.final_url for p in self.pages if p.content is not None})

    @property
    def state(self) -> CrawlState:
        """``failed``: nothing could be audited; ``partial``: findings cover a subset."""
        if self.audited_page_count == 0:
            return "failed"
        return "partial" if self.state_reasons else "complete"

    @property
    def state_reasons(self) -> tuple[str, ...]:
        reasons = []
        if self.audited_page_count == 0:
            start = next((p for p in self.pages if p.requested_url == self.start_url), None)
            if start is not None and start.error:
                reasons.append(f"start URL failed: {start.error_kind}")
            elif start is not None:
                reasons.append(f"start URL returned HTTP {start.status_code}")
            elif self.start_url in self.skipped:
                reasons.append(f"start URL skipped: {self.skipped[self.start_url]}")
            else:
                reasons.append("no page could be fetched")
        if self.stop_reason:
            reasons.append(f"crawl stopped early: {self.stop_reason}")
        if self.pending:
            reasons.append(f"{len(self.pending)} discovered URL(s) not crawled")
        if self.unchecked_links:
            reasons.append(f"{len(self.unchecked_links)} link target(s) not checked")
        unverifiable = [
            r for r in self.link_checks.values()
            if not is_definitive_response(r.status_code, r.error_kind)
        ]  # fmt: skip
        if unverifiable:
            reasons.append(
                f"{len(unverifiable)} link target(s) could not be verified "
                "(401/403/429 or a network-level failure)"
            )
        unretrieved = self.unretrieved_pages
        if unretrieved and self.audited_page_count:
            kinds = Counter(p.unretrieved_reason for p in unretrieved)
            breakdown = ", ".join(f"{kind}: {n}" for kind, n in kinds.most_common())
            reasons.append(
                f"{len(unretrieved)} of {len(self.pages)} crawled page(s) could not be "
                f"retrieved or analyzed ({breakdown})"
            )
        if not self.sitemaps.complete and self.sitemaps.checked:
            reasons.append("sitemap list incomplete")
        if any(p.likely_client_rendered for p in self.pages):
            reasons.append("some pages appear client-rendered (JavaScript not executed)")
        return tuple(reasons)
