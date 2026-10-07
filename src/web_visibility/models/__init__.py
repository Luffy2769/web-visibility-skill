"""Domain models shared by the crawler, analyzers, scoring and reporters.

These modules depend only on each other, ``config`` and the pure parsers
(``robots``, ``urls``) - never on the crawler - so future front ends (repository
audits, report comparison) can construct and consume them directly.
"""

from web_visibility.models.crawl import CrawlResult, CrawlState
from web_visibility.models.issue import Category, Issue, Rule, Severity
from web_visibility.models.page import (
    DEFINITIVE_ERROR_KINDS,
    HTML_CONTENT_TYPES,
    INDEXING_AGENTS,
    UNRETRIEVED_STATUSES,
    CanonicalLink,
    DiscoverySource,
    FetchResult,
    Heading,
    Image,
    JsonLdBlock,
    Link,
    Page,
    PageContent,
    RedirectHop,
    RobotsMeta,
    is_definitive_response,
)
from web_visibility.models.robots import RobotsResult, RobotsState, classify_robots_response
from web_visibility.models.sitemap import SitemapFile, SitemapKind, SitemapResult, SitemapSource

__all__ = [
    "DEFINITIVE_ERROR_KINDS",
    "HTML_CONTENT_TYPES",
    "INDEXING_AGENTS",
    "UNRETRIEVED_STATUSES",
    "CanonicalLink",
    "Category",
    "CrawlResult",
    "CrawlState",
    "DiscoverySource",
    "FetchResult",
    "Heading",
    "Image",
    "Issue",
    "JsonLdBlock",
    "Link",
    "Page",
    "PageContent",
    "RedirectHop",
    "RobotsMeta",
    "RobotsResult",
    "RobotsState",
    "Rule",
    "Severity",
    "SitemapFile",
    "SitemapKind",
    "SitemapResult",
    "SitemapSource",
    "classify_robots_response",
    "is_definitive_response",
]
