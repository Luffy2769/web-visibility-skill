"""Sitemap discovery results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SitemapKind = Literal["urlset", "sitemapindex", "text"]
SitemapSource = Literal["robots", "default", "index"]


@dataclass(frozen=True, slots=True)
class SitemapFile:
    url: str
    source: SitemapSource
    """``robots``: declared in robots.txt; ``index``: listed by a sitemap index;
    ``default``: the guessed ``/sitemap.xml`` location (only a guess)."""
    status_code: int | None = None
    error: str | None = None
    error_kind: str | None = None
    parse_error: str | None = None
    kind: SitemapKind | None = None
    url_count: int = 0
    invalid_urls: tuple[str, ...] = ()
    duplicate_urls: tuple[str, ...] = ()
    cross_host_urls: tuple[str, ...] = ()
    children: tuple[str, ...] = ()
    looks_like_html: bool = False
    """The response was an HTML page (typically an SPA or CMS catch-all route)."""

    @property
    def fetched(self) -> bool:
        return self.error is None and self.status_code is not None and 200 <= self.status_code < 300

    @property
    def valid(self) -> bool:
        return self.fetched and self.parse_error is None

    @property
    def guessed(self) -> bool:
        return self.source == "default"


@dataclass(frozen=True, slots=True)
class SitemapResult:
    files: tuple[SitemapFile, ...] = ()
    urls: tuple[str, ...] = ()
    """Unique, normalized page URLs listed across all valid sitemaps."""
    url_limit_reached: bool = False
    file_limit_reached: bool = False
    checked: bool = True
    """``False`` when sitemap discovery was skipped (e.g. the host was unreachable)."""

    @property
    def found(self) -> bool:
        return any(f.valid for f in self.files)

    @property
    def complete(self) -> bool:
        """True only if every declared/indexed sitemap was read within the limits.

        Findings that depend on the full URL list (``page-not-in-sitemap``) must not
        run on an incomplete sitemap. A failed *guess* does not make it incomplete.
        """
        if not self.checked or self.url_limit_reached or self.file_limit_reached:
            return False
        return all(f.valid for f in self.files if not f.guessed)
