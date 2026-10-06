"""Fetched responses and parsed pages.

Models are frozen dataclasses: once the crawler has observed a page, nothing
downstream can silently rewrite what was observed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})

# Crawlers whose page-level robots directives are evaluated individually.
INDEXING_AGENTS = ("googlebot", "bingbot")
# Meta tag names read as robots directives (``<meta name="googlebot" ...>``).
ROBOTS_META_NAMES = frozenset({"robots", *INDEXING_AGENTS})
# Directives that legitimately contain a colon without naming a user agent.
_UNSCOPED_COLON_DIRECTIVES = frozenset(
    {"unavailable_after", "max-snippet", "max-image-preview", "max-video-preview"}
)

CanonicalSource = Literal["head", "head-implicitly-closed", "body", "http-header"]


class DiscoverySource(StrEnum):
    """How the crawler found a URL."""

    START = "start"
    LINK = "link"
    SITEMAP = "sitemap"


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RedirectHop:
    """One redirect response: ``url`` answered with ``status_code``."""

    url: str
    status_code: int


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Raw outcome of fetching a URL (after following safe redirects)."""

    requested_url: str
    final_url: str
    status_code: int | None
    headers: dict[str, str]
    body: bytes = b""
    redirect_chain: tuple[RedirectHop, ...] = ()
    error: str | None = None
    error_kind: str | None = None
    truncated: bool = False
    """The body exceeded the size limit and was cut off (the response is incomplete)."""
    retries: int = 0

    @property
    def content_type(self) -> str | None:
        raw = self.headers.get("content-type", "")
        value = raw.split(";", 1)[0].strip().lower()
        return value or None

    @property
    def charset(self) -> str | None:
        for part in self.headers.get("content-type", "").split(";")[1:]:
            key, _, value = part.partition("=")
            if key.strip().lower() == "charset" and value.strip():
                return value.strip().strip("\"'")
        return None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None


# --------------------------------------------------------------------------- #
# Parsed page content
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Link:
    """An ``<a href>`` that resolves to an HTTP(S) URL."""

    url: str
    """Absolute, normalized target (fragment removed)."""
    raw_href: str
    text: str
    """Accessible name: visible text, else aria-label / title / child image alt."""
    rel: tuple[str, ...] = ()
    internal: bool = False
    """True when the target is on the same host as the page."""

    @property
    def nofollow(self) -> bool:
        return "nofollow" in self.rel


@dataclass(frozen=True, slots=True)
class Image:
    """An ``<img>`` element. ``alt is None`` means the attribute is absent."""

    src: str | None
    alt: str | None
    width: str | None = None
    height: str | None = None
    loading: str | None = None
    in_link: bool = False


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    text: str


@dataclass(frozen=True, slots=True)
class JsonLdBlock:
    """One ``<script type="application/ld+json">`` block."""

    raw: str
    data: Any = None
    error: str | None = None
    types: tuple[str, ...] = ()
    """Top-level ``@type`` values (including ``@graph`` members)."""
    missing_context: bool = False
    missing_type: bool = False

    @property
    def valid(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class CanonicalLink:
    """A canonical declaration and where it was found.

    ``head``: inside ``<head>`` before any body content - the normal place.
    ``head-implicitly-closed``: written inside ``<head>`` but after an element that
    makes HTML parsers close the head early (``<div>``, ``<img>``...), so browsers
    and search engines may treat it as body content.
    ``body``: inside ``<body>``; generally not honoured.
    ``http-header``: ``Link: <url>; rel="canonical"`` response header.
    """

    href: str | None
    source: CanonicalSource


@dataclass(frozen=True, slots=True)
class RobotsMeta:
    """A ``<meta name="robots|googlebot|bingbot" content="...">`` element."""

    name: str
    content: str


@dataclass(frozen=True, slots=True)
class PageContent:
    """Everything extracted from an HTML document."""

    titles: tuple[str, ...] = ()
    meta_descriptions: tuple[str, ...] = ()
    canonical_links: tuple[CanonicalLink, ...] = ()
    robots_meta: tuple[RobotsMeta, ...] = ()
    headings: tuple[Heading, ...] = ()
    links: tuple[Link, ...] = ()
    images: tuple[Image, ...] = ()
    json_ld: tuple[JsonLdBlock, ...] = ()
    subresources: tuple[str, ...] = ()
    """Absolute URLs of images, scripts, stylesheets, iframes and media."""
    has_microdata: bool = False
    has_rdfa: bool = False
    lang: str | None = None
    head_anomalies: tuple[str, ...] = ()
    """Body-only elements found inside ``<head>`` (e.g. ``"<div>"``)."""
    visible_text_length: int = 0
    """Characters of visible body text (scripts, styles and templates excluded)."""
    render_signals: tuple[str, ...] = ()
    """Signals that the document is a client-rendered application shell."""
    likely_client_rendered: bool = False

    @property
    def meta_robots(self) -> tuple[str, ...]:
        """Contents of ``<meta name="robots">`` (directives for all crawlers)."""
        return tuple(m.content for m in self.robots_meta if m.name == "robots")

    @property
    def canonicals(self) -> tuple[str | None, ...]:
        """Raw hrefs of every HTML ``rel=canonical`` (any placement)."""
        return tuple(c.href for c in self.canonical_links)


# --------------------------------------------------------------------------- #
# Robots directives (meta robots / X-Robots-Tag)
# --------------------------------------------------------------------------- #


def split_directives(value: str) -> set[str]:
    """``"noindex, NoFollow"`` -> ``{"noindex", "nofollow"}`` (``none`` expands)."""
    found = {d.strip().lower() for d in value.split(",") if d.strip()}
    if "none" in found:
        found |= {"noindex", "nofollow"}
    return found


def parse_x_robots_tag(header: str | None) -> dict[str | None, set[str]]:
    """Parse ``X-Robots-Tag`` into ``{agent or None: directives}``.

    ``None`` holds directives for all crawlers. ``googlebot: noindex, nofollow``
    scopes every following directive on that header line to ``googlebot``.
    Multiple headers are stored newline-separated by the fetcher.
    """
    parsed: dict[str | None, set[str]] = {}
    for line in (header or "").splitlines():
        agent: str | None = None
        for part in line.split(","):
            part = part.strip()
            prefix, sep, rest = part.partition(":")
            prefix = prefix.strip().lower()
            if sep and prefix not in _UNSCOPED_COLON_DIRECTIVES and " " not in prefix:
                agent, part = prefix, rest.strip()
            if part:
                parsed.setdefault(agent, set()).update(split_directives(part))
    return parsed


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Page:
    """A crawled URL: what was requested, what came back, and what it contained."""

    requested_url: str
    final_url: str
    status_code: int | None
    content_type: str | None
    response_headers: dict[str, str]
    discovered_via: DiscoverySource
    depth: int
    redirect_chain: tuple[RedirectHop, ...] = ()
    error: str | None = None
    error_kind: str | None = None
    truncated: bool = False
    content: PageContent | None = None
    """``None`` unless the response was a complete, successful HTML document."""

    # --- response ---------------------------------------------------------- #

    @property
    def is_html(self) -> bool:
        return self.content_type in HTML_CONTENT_TYPES

    @property
    def is_success(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def likely_client_rendered(self) -> bool:
        return bool(self.content and self.content.likely_client_rendered)

    # --- metadata ---------------------------------------------------------- #

    @property
    def title(self) -> str | None:
        return self.content.titles[0] if self.content and self.content.titles else None

    @property
    def meta_description(self) -> str | None:
        if self.content and self.content.meta_descriptions:
            return self.content.meta_descriptions[0]
        return None

    @property
    def header_canonicals(self) -> tuple[CanonicalLink, ...]:
        return tuple(
            CanonicalLink(href, "http-header")
            for href in parse_link_header_canonicals(self.response_headers.get("link"))
        )

    @property
    def canonical_candidates(self) -> tuple[CanonicalLink, ...]:
        """Every canonical declaration: HTML (any placement) then HTTP header."""
        html = self.content.canonical_links if self.content else ()
        return html + self.header_canonicals

    @property
    def effective_canonicals(self) -> tuple[CanonicalLink, ...]:
        """Declarations normally honoured: HTML ``<head>`` first, then the HTTP header.

        Body canonicals and ones after an implicit ``</head>`` are excluded; they
        are reported separately as placement problems.
        """
        head = tuple(c for c in self.canonical_candidates if c.source == "head")
        return head + self.header_canonicals

    @property
    def canonical(self) -> str | None:
        """Raw href of the effective canonical (precedence: ``<head>``, then header)."""
        effective = self.effective_canonicals
        return effective[0].href if effective else None

    # --- structure --------------------------------------------------------- #

    @property
    def h1s(self) -> tuple[str, ...]:
        return tuple(h.text for h in self.headings if h.level == 1)

    @property
    def headings(self) -> tuple[Heading, ...]:
        return self.content.headings if self.content else ()

    @property
    def internal_links(self) -> tuple[Link, ...]:
        return tuple(link for link in self.links if link.internal)

    @property
    def external_links(self) -> tuple[Link, ...]:
        return tuple(link for link in self.links if not link.internal)

    @property
    def links(self) -> tuple[Link, ...]:
        return self.content.links if self.content else ()

    @property
    def images(self) -> tuple[Image, ...]:
        return self.content.images if self.content else ()

    @property
    def structured_data(self) -> tuple[JsonLdBlock, ...]:
        return self.content.json_ld if self.content else ()

    # --- robots directives ------------------------------------------------- #

    @property
    def x_robots_tag(self) -> str | None:
        return self.response_headers.get("x-robots-tag")

    def directives_for(self, agent: str | None = None) -> frozenset[str]:
        """Directives that apply to ``agent`` (``None`` = those addressed to all crawlers).

        For a named agent this is the union of the generic directives and the ones
        addressed to it (``<meta name="googlebot">``, ``X-Robots-Tag: googlebot: ...``).
        """
        wanted = {None, agent.lower()} if agent else {None}
        directives: set[str] = set()
        if self.content:
            for meta in self.content.robots_meta:
                name = None if meta.name == "robots" else meta.name
                if name in wanted:
                    directives |= split_directives(meta.content)
        for scope, values in parse_x_robots_tag(self.x_robots_tag).items():
            if scope in wanted:
                directives |= values
        return frozenset(directives)

    @property
    def robots_directives(self) -> frozenset[str]:
        """Directives addressed to all crawlers."""
        return self.directives_for(None)

    @property
    def is_noindex(self) -> bool:
        """``noindex`` (or ``none``) addressed to all crawlers."""
        return "noindex" in self.robots_directives

    @property
    def noindex_agents(self) -> tuple[str, ...]:
        """Evaluated crawlers (``INDEXING_AGENTS``) instructed not to index the page."""
        return tuple(a for a in INDEXING_AGENTS if "noindex" in self.directives_for(a))

    @property
    def noindex_for_any_agent(self) -> bool:
        return self.is_noindex or bool(self.noindex_agents)


def parse_link_header_canonicals(header: str | None) -> tuple[str, ...]:
    """Targets of ``rel="canonical"`` entries in an HTTP ``Link`` header."""
    if not header:
        return ()
    found: list[str] = []
    for entry in _split_link_header(header):
        target, _, params = entry.partition(">")
        target = target.strip().lstrip("<").strip()
        rels: set[str] = set()
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "rel":
                rels |= {r.lower() for r in value.strip().strip("\"'").split()}
        if "canonical" in rels and target:
            found.append(target)
    return tuple(found)


def _split_link_header(header: str) -> list[str]:
    """Split on commas that are not inside ``<...>`` or quotes."""
    parts: list[str] = []
    current: list[str] = []
    depth, quoted = 0, False
    for char in header:
        if char == '"':
            quoted = not quoted
        elif char == "<" and not quoted:
            depth += 1
        elif char == ">" and not quoted:
            depth = max(0, depth - 1)
        if char == "," and depth == 0 and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]
