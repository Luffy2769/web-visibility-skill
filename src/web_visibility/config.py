"""Crawl and fetch configuration (plain values, no behaviour)."""

from __future__ import annotations

from dataclasses import dataclass, field

from web_visibility import __version__

_MAJOR_MINOR = ".".join(__version__.split(".")[:2])
USER_AGENT = (
    f"WebVisibilitySkill/{_MAJOR_MINOR} (+https://github.com/luffy2769/web-visibility-skill)"
)
ROBOTS_USER_AGENT_TOKEN = "WebVisibilitySkill"


@dataclass(frozen=True, slots=True)
class FetchConfig:
    timeout: float = 10.0
    """Seconds allowed for DNS resolution, connecting, and each read."""
    max_redirects: int = 5
    max_bytes: int = 5_000_000
    """Maximum *decompressed* body size; larger responses are cut off and flagged."""
    delay: float = 0.25
    """Minimum seconds between the start of two consecutive requests."""
    allow_private: bool = False
    user_agent: str = USER_AGENT
    max_retry_wait: float = 30.0
    """Longest ``Retry-After`` (429/503) honoured with one retry; longer waits give up."""
    transient_retries: int = 1
    """Retries after timeouts/connection errors, for requests that opt in."""

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.max_redirects < 0:
            raise ValueError("max_redirects must be >= 0")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if self.delay < 0 or self.max_retry_wait < 0 or self.transient_retries < 0:
            raise ValueError("delay, max_retry_wait and transient_retries must be >= 0")


@dataclass(frozen=True, slots=True)
class CrawlConfig:
    max_pages: int = 20
    max_link_checks: int = 50
    check_external: bool = False
    max_external_checks: int = 25
    respect_robots: bool = True
    max_sitemaps: int = 10
    max_sitemap_urls: int = 50_000
    max_crawl_delay: float = 30.0
    """Largest robots.txt ``Crawl-delay`` honoured; above it only the start URL is fetched."""
    fetch: FetchConfig = field(default_factory=FetchConfig)

    def __post_init__(self) -> None:
        if self.max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if self.max_link_checks < 0 or self.max_external_checks < 0:
            raise ValueError("link check limits must be >= 0")
        if self.max_sitemaps < 0 or self.max_sitemap_urls < 0 or self.max_crawl_delay < 0:
            raise ValueError("sitemap and crawl-delay limits must be >= 0")
