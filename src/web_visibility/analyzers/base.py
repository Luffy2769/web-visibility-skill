"""Shared, read-only view of a crawl that every analyzer receives."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from web_visibility.models import CrawlResult, Issue, Page, is_definitive_response
from web_visibility.urls import same_host

Analyzer = Callable[["AuditContext"], list[Issue]]

SAMPLE_SIZE = 5


@dataclass(frozen=True, slots=True)
class TargetStatus:
    """What is known about a URL's response, from the crawl or a link check."""

    url: str
    status_code: int | None
    final_url: str
    redirected: bool
    error: str | None
    error_kind: str | None

    @property
    def definitive(self) -> bool:
        """The response proves the target works or is broken (see ``is_definitive_response``)."""
        return is_definitive_response(self.status_code, self.error_kind)


@dataclass(frozen=True, slots=True)
class LinkCoverage:
    """How many distinct link targets exist and how many have a *definitive* response.

    ``*_checked`` counts only answers that prove a target works or is broken.
    ``*_unverifiable`` counts targets that were requested but answered with a
    timeout, network error, SSRF block or 401/403/407/429: they are neither
    passing nor failing, and are never counted as checked.
    """

    internal_discovered: int
    internal_checked: int
    external_discovered: int
    external_checked: int
    not_applicable: int
    """Targets deliberately not fetched (robots.txt disallowed)."""
    internal_unverifiable: int = 0
    external_unverifiable: int = 0


@dataclass(frozen=True)
class AuditContext:
    crawl: CrawlResult
    pages: tuple[Page, ...]
    """Auditable pages: successful same-origin HTML, de-duplicated by final URL."""
    page_index: dict[str, Page]
    """Every crawled page keyed by both its requested and its final URL."""
    inlinks: dict[str, frozenset[str]]
    """Target URL -> final URLs of auditable pages linking to it (self-links excluded)."""

    @classmethod
    def build(cls, crawl: CrawlResult) -> AuditContext:
        index: dict[str, Page] = {}
        auditable: dict[str, Page] = {}
        for page in crawl.pages:
            index.setdefault(page.requested_url, page)
            index.setdefault(page.final_url, page)
            if page.content is not None:
                auditable.setdefault(page.final_url, page)

        sources: defaultdict[str, set[str]] = defaultdict(set)
        for page in auditable.values():
            for link in page.internal_links:
                if link.url != page.final_url and link.url != page.requested_url:
                    sources[link.url].add(page.final_url)
        return cls(
            crawl=crawl,
            pages=tuple(auditable.values()),
            page_index=index,
            inlinks={url: frozenset(found) for url, found in sources.items()},
        )

    # ------------------------------------------------------------------ #

    @property
    def start_url(self) -> str:
        return self.crawl.start_url

    @property
    def site_url(self) -> str:
        """Effective site URL (the start URL's same-site redirect target, if any).
        Use it for internal/external decisions; use ``start_url`` for "the page asked for"."""
        return self.crawl.site_url

    @property
    def start_page(self) -> Page | None:
        return self.page_index.get(self.start_url)

    def is_start(self, page: Page) -> bool:
        return page.requested_url == self.start_url or page.final_url in (
            self.start_url,
            self.site_url,
        )

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def partial(self) -> bool:
        """True when the crawl stopped at ``max_pages`` with URLs still pending."""
        return self.crawl.limit_reached

    def target_status(self, url: str) -> TargetStatus | None:
        """Response information for ``url`` if it was crawled or link-checked."""
        page = self.page_index.get(url)
        if page is not None:
            # A URL found as the *final* URL of a redirect is itself a 2xx answer.
            redirected = bool(page.redirect_chain) and page.requested_url == url
            return TargetStatus(
                url, page.status_code, page.final_url, redirected, page.error, page.error_kind
            )
        check = self.crawl.link_checks.get(url)
        if check is not None:
            return TargetStatus(
                url,
                check.status_code,
                check.final_url,
                bool(check.redirect_chain),
                check.error,
                check.error_kind,
            )
        return None

    def link_targets(self) -> dict[str, bool]:
        """Distinct link targets on audited pages -> ``True`` if internal (self-links excluded)."""
        targets: dict[str, bool] = {}
        for page in self.pages:
            for link in page.links:
                if link.url not in (page.final_url, page.requested_url):
                    targets.setdefault(link.url, same_host(link.url, self.site_url))
        return targets

    def link_coverage(self) -> LinkCoverage:
        counts = {"int": 0, "int_ok": 0, "int_unv": 0, "ext": 0, "ext_ok": 0, "ext_unv": 0, "na": 0}
        for target, internal in self.link_targets().items():
            if self.crawl.skipped.get(target, "").startswith("robots"):
                counts["na"] += 1
                continue
            key = "int" if internal else "ext"
            counts[key] += 1
            status = self.target_status(target)
            if status is not None:
                counts[f"{key}_ok" if status.definitive else f"{key}_unv"] += 1
        return LinkCoverage(counts["int"], counts["int_ok"], counts["ext"], counts["ext_ok"],
                            counts["na"], counts["int_unv"], counts["ext_unv"])  # fmt: skip

    def inbound_sources(self, page: Page) -> frozenset[str]:
        """Auditable pages linking to ``page`` (by requested or final URL)."""
        found = self.inlinks.get(page.requested_url, frozenset()) | self.inlinks.get(
            page.final_url, frozenset()
        )
        return found - {page.final_url}


def sample(items: Iterable[str], limit: int = SAMPLE_SIZE) -> list[str]:
    """First ``limit`` unique items, preserving order."""
    return list(dict.fromkeys(items))[:limit]


def joined_list(items: Iterable[str], limit: int = SAMPLE_SIZE, quote: bool = False) -> str:
    """``a, b, c (+N more)`` over the unique items."""
    values = list(dict.fromkeys(items))
    shown = ", ".join(f'"{v}"' if quote else v for v in values[:limit])
    return shown + (f" (+{len(values) - limit} more)" if len(values) > limit else "")


def quoted_list(items: Iterable[str], limit: int = SAMPLE_SIZE) -> str:
    return joined_list(items, limit, quote=True)


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    word = singular if count == 1 else (plural_form or singular + "s")
    return f"{count} {word}"
