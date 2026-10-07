"""HTML extraction: turn a response body into a :class:`PageContent`.

Uses Python's built-in ``html.parser`` through BeautifulSoup. It tolerates the
malformed markup common on real sites and has no compiled dependencies.
Extraction is purely factual; judging the results is the analyzers' job.

Complexity: the document tree is walked **once**, iteratively, carrying the
relevant ancestor context (inside ``<head>``, ``<noscript>``, ``<a>``...) as bit
flags. Per-element text extraction is bounded. A hostile, deeply nested page
therefore costs time linear in its size (pages are already size-limited), not
quadratic as naive ``find_parent`` / ``get_text`` calls per element would be.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, Tag
from bs4.element import NavigableString, PageElement

from web_visibility.models import (
    CanonicalLink,
    Heading,
    Image,
    JsonLdBlock,
    Link,
    PageContent,
    RobotsMeta,
)
from web_visibility.models.page import ROBOTS_META_NAMES, CanonicalSource
from web_visibility.urls import host_of, is_skipped_scheme, normalize_url

_WHITESPACE = re.compile(r"\s+")
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_JSON_LD_TYPE = re.compile(r"^\s*application/ld\+json\s*(;.*)?$", re.IGNORECASE)
_SUBRESOURCE_ATTRS = {
    "img": "src",
    "script": "src",
    "iframe": "src",
    "source": "src",
    "video": "src",
    "audio": "src",
    "embed": "src",
}
_MAX_RAW_JSON_LD = 20_000  # characters kept per block for reporting

# Bounds for per-element text extraction (headings, links, titles, mount points).
_TEXT_NODE_BUDGET = 64
_TEXT_CHAR_BUDGET = 300
_NO_TEXT_SUBTREES = frozenset({"script", "style", "template"})
_SVG_TITLE_SCAN = 4  # an accessible <svg> puts <title> first; never scan all its paths

# Elements that make an HTML parser close <head> implicitly and start the body.
# Python's html.parser does not do this, so placement is tracked explicitly.
_BODY_ONLY = frozenset(
    {
        "a", "article", "aside", "audio", "b", "blockquote", "br", "button", "canvas",
        "div", "dl", "em", "fieldset", "figure", "footer", "form", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "hr", "i", "iframe", "img", "input", "label", "li",
        "main", "nav", "ol", "p", "picture", "pre", "section", "select", "span",
        "strong", "table", "textarea", "ul", "video",
    }
)  # fmt: skip

# Ancestor-context flags carried through the single tree walk.
_IN_HEAD = 1
_IN_BODY = 2
_IN_OPAQUE = 4  # noscript / template / svg: not treated as document markup
_INVISIBLE = 8  # text here is not visible page text
_IN_SVG = 16
_IN_NOSCRIPT = 32
_IN_ANCHOR = 64
_TAG_FLAGS = {
    "head": _IN_HEAD | _INVISIBLE,
    "body": _IN_BODY,
    "noscript": _IN_OPAQUE | _INVISIBLE | _IN_NOSCRIPT,
    "template": _IN_OPAQUE | _INVISIBLE,
    "svg": _IN_OPAQUE | _INVISIBLE | _IN_SVG,
    "script": _INVISIBLE,
    "style": _INVISIBLE,
    "title": _INVISIBLE,
    "a": _IN_ANCHOR,
}

# Client-rendered shell detection (see _render_signals).
_MOUNT_IDS = frozenset(
    {
        "root",
        "app",
        "__next",
        "__nuxt",
        "___gatsby",
        "svelte",
        "react-root",
        "app-root",
        "application",
    }
)
_SHELL_TEXT_THRESHOLD = 150
_CONTENT_ELEMENTS = frozenset(
    {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "article", "section", "main", "img",
     "table", "blockquote"}
)  # fmt: skip
# "few-content-elements" is recorded as supporting evidence only: tiny static pages
# (e.g. a one-paragraph landing page with an analytics script) also have few elements.
_STRUCTURAL_SIGNALS = frozenset(
    {"empty-mount-element", "empty-body-containers", "noscript-javascript-notice"}
)

Walked = list[tuple[PageElement, int]]
Tags = list[tuple[Tag, int]]


def parse_html(body: bytes | str, url: str, *, encoding: str | None = None) -> PageContent:
    """Extract the Phase 01 facts from an HTML document located at ``url``."""
    soup = (
        BeautifulSoup(body, "html.parser", from_encoding=encoding)
        if isinstance(body, bytes)
        else BeautifulSoup(body, "html.parser")
    )
    base_url = _base_url(soup, url)
    page_host = host_of(url)
    nodes = _walk(soup)
    tags: Tags = [(node, flags) for node, flags in nodes if isinstance(node, Tag)]

    html = soup.find("html")
    canonical_links, robots_meta, head_anomalies = _document_order_scan(tags)
    visible_text = _visible_text_length(nodes)
    signals = _render_signals(soup, tags, visible_text)
    return PageContent(
        titles=tuple(_bounded_text(t) for t, f in tags if t.name == "title" and not f & _IN_SVG),
        meta_descriptions=_meta_contents(tags, "description"),
        canonical_links=canonical_links,
        robots_meta=robots_meta,
        head_anomalies=head_anomalies,
        visible_text_length=visible_text,
        render_signals=signals,
        likely_client_rendered=is_likely_shell(signals),
        headings=tuple(
            Heading(level=int(t.name[1]), text=_bounded_text(t))
            for t, _ in tags
            if t.name in _HEADING_TAGS
        ),
        links=_links(tags, base_url, page_host),
        images=_images(tags, base_url),
        json_ld=_json_ld(tags),
        subresources=_subresources(tags, base_url),
        has_microdata=any(_has(t, "itemscope") for t, _ in tags),
        has_rdfa=any(_has(t, "typeof") for t, _ in tags),
        lang=_attr(html, "lang") if isinstance(html, Tag) else None,
    )


def clean_text(value: str) -> str:
    """Collapse whitespace runs into single spaces and trim."""
    return _WHITESPACE.sub(" ", value).strip()


# --------------------------------------------------------------------------- #
# Tree walk
# --------------------------------------------------------------------------- #


def _walk(soup: BeautifulSoup) -> Walked:
    """Every node in document order with the flags of its *ancestors* (iterative)."""
    out: Walked = []
    stack: list[tuple[PageElement, int]] = [(soup, 0)]
    while stack:
        node, flags = stack.pop()
        out.append((node, flags))
        if isinstance(node, Tag):
            child_flags = flags | _TAG_FLAGS.get(node.name, 0)
            stack.extend((child, child_flags) for child in reversed(node.contents))
    return out


def _bounded_descendants(tag: Tag) -> Iterator[PageElement]:
    """Descendants in document order, at most ``_TEXT_NODE_BUDGET`` of them.

    Not ``tag.descendants``: bs4 first walks to the element's last descendant,
    which is O(depth) per call and makes nested markup quadratic.

    The budget is not spent inside subtrees that hold no readable text: scripts,
    styles and templates are skipped, and an ``<svg>`` contributes only its
    ``<title>``. Otherwise one icon (``<h1><svg>40 paths</svg>Acme</h1>``, a common
    logo pattern) exhausts the budget and the heading reads as empty.
    """
    stack: list[PageElement] = list(reversed(tag.contents))
    for _ in range(_TEXT_NODE_BUDGET):
        if not stack:
            return
        node = stack.pop()
        yield node
        if not isinstance(node, Tag) or node.name in _NO_TEXT_SUBTREES:
            continue
        if node.name == "svg":
            titles = [
                c for c in node.contents[:_SVG_TITLE_SCAN] if getattr(c, "name", None) == "title"
            ]
            stack.extend(reversed(titles))
        else:
            stack.extend(reversed(node.contents))


def _bounded_text(tag: PageElement) -> str:
    """Text of ``tag``, reading at most a fixed number of nodes and characters."""
    if not isinstance(tag, Tag):
        return ""
    parts: list[str] = []
    chars = 0
    for node in _bounded_descendants(tag):
        if isinstance(node, NavigableString) and not isinstance(
            node, (Comment, Declaration, Doctype)
        ):
            parts.append(str(node))
            chars += len(node)
            if chars >= _TEXT_CHAR_BUDGET:
                break
    return clean_text(" ".join(parts))[:_TEXT_CHAR_BUDGET]


# --------------------------------------------------------------------------- #
# Extractors
# --------------------------------------------------------------------------- #


def _meta_contents(tags: Tags, name: str) -> tuple[str, ...]:
    return tuple(
        clean_text(_attr(t, "content") or "")
        for t, _ in tags
        if t.name == "meta" and (_attr(t, "name") or "").strip().lower() == name
    )


def _document_order_scan(
    tags: Tags,
) -> tuple[tuple[CanonicalLink, ...], tuple[RobotsMeta, ...], tuple[str, ...]]:
    """Canonicals (with placement), robots meta tags and head anomalies, in order.

    A browser closes ``<head>`` at the first body-only element (``<div>``, ``<img>``,
    ...). Canonicals written after that point are recorded as
    ``head-implicitly-closed`` rather than trusted as head metadata.
    """
    canonicals: list[CanonicalLink] = []
    robots_meta: list[RobotsMeta] = []
    anomalies: list[str] = []
    body_started = False
    for element, flags in tags:
        if flags & _IN_OPAQUE:
            continue
        name = element.name
        in_head = bool(flags & _IN_HEAD)
        if name == "body":
            body_started = True
        elif name in _BODY_ONLY:
            if in_head and not body_started:
                anomalies.append(f"<{name}>")
            body_started = True
        elif name == "link" and "canonical" in _rel(element):
            href = _attr(element, "href")
            source: CanonicalSource
            if flags & _IN_BODY or (body_started and not in_head):
                source = "body"
            elif body_started:
                source = "head-implicitly-closed"
            else:
                source = "head"
            canonicals.append(CanonicalLink(href.strip() if href is not None else None, source))
        elif name == "meta":
            meta_name = (_attr(element, "name") or "").strip().lower()
            if meta_name in ROBOTS_META_NAMES:
                content = clean_text(_attr(element, "content") or "")
                robots_meta.append(RobotsMeta(meta_name, content))
    return tuple(canonicals), tuple(robots_meta), tuple(dict.fromkeys(anomalies))


def _visible_text_length(nodes: Walked) -> int:
    total = 0
    for node, flags in nodes:
        if flags & _INVISIBLE or not isinstance(node, NavigableString):
            continue
        if isinstance(node, (Comment, Declaration, Doctype)):
            continue
        total += len(clean_text(str(node)))
    return total


def _render_signals(soup: BeautifulSoup, tags: Tags, visible_text: int) -> tuple[str, ...]:
    """Evidence that the document is an application shell rendered by JavaScript.

    No single signal is decisive (custom mount ids are common), so several are
    collected and combined in :func:`is_likely_shell`.
    """
    signals: list[str] = []
    if visible_text < _SHELL_TEXT_THRESHOLD:
        signals.append(f"little-visible-text:{visible_text}")

    scripts = [t for t, _ in tags if t.name == "script"]
    external = [
        t for t in scripts if _attr(t, "src") or (_attr(t, "type") or "").lower() == "module"
    ]
    content = sum(1 for t, f in tags if t.name in _CONTENT_ELEMENTS and not f & _IN_OPAQUE)
    if external and len(scripts) >= content:
        signals.append(f"script-driven:{len(scripts)}-scripts")
    if content <= 3:
        signals.append(f"few-content-elements:{content}")

    marker = _empty_mount_marker(tags)
    if marker:
        signals.append(f"empty-mount-element:{marker}")

    body = soup.find("body")
    if isinstance(body, Tag):
        children = [
            c
            for c in body.find_all(True, recursive=False)
            if c.name not in ("script", "noscript", "style", "link", "template")
        ]
        if 0 < len(children) <= 3 and all(not _bounded_text(c) for c in children):
            signals.append(f"empty-body-containers:{len(children)}")

    for t, _ in tags:
        if t.name == "noscript" and "javascript" in _bounded_text(t).lower():
            signals.append("noscript-javascript-notice")
            break
    return tuple(signals)


def _empty_mount_marker(tags: Tags) -> str | None:
    for tag, _ in tags:
        tag_id = (_attr(tag, "id") or "").lower()
        if tag_id in _MOUNT_IDS:
            marker = f"#{tag_id}"
        elif tag.has_attr("data-reactroot"):
            marker = "data-reactroot"
        elif tag.has_attr("ng-version"):
            marker = "ng-version"
        elif tag.name in ("app-root", "ion-app"):
            marker = f"<{tag.name}>"
        else:
            continue
        if not _bounded_text(tag):
            return marker
    return None


def is_likely_shell(signals: tuple[str, ...]) -> bool:
    """Little visible text AND script-driven AND at least one structural signal."""
    kinds = {s.split(":", 1)[0] for s in signals}
    return {"little-visible-text", "script-driven"} <= kinds and bool(kinds & _STRUCTURAL_SIGNALS)


def _links(tags: Tags, base_url: str, page_host: str) -> tuple[Link, ...]:
    links = []
    for tag, _ in tags:
        if tag.name != "a":
            continue
        href = _attr(tag, "href")
        if href is None or is_skipped_scheme(href):
            continue
        target = normalize_url(href, base_url)
        if target is None:
            continue
        links.append(
            Link(
                url=target,
                raw_href=href,
                text=_accessible_name(tag),
                rel=_rel(tag),
                internal=host_of(target) == page_host,
            )
        )
    return tuple(links)


def _images(tags: Tags, base_url: str) -> tuple[Image, ...]:
    images = []
    for tag, flags in tags:
        # <noscript> fallbacks duplicate lazy-loaded images; skip them.
        if tag.name != "img" or flags & _IN_NOSCRIPT:
            continue
        src = _attr(tag, "src")
        resolved = normalize_url(src, base_url) if src else None
        if src and resolved is None and len(src) > 80:
            src = src[:80] + "..."  # e.g. inline data: URIs; keep reports readable
        images.append(
            Image(
                src=resolved or src,
                alt=_attr(tag, "alt"),
                width=_attr(tag, "width"),
                height=_attr(tag, "height"),
                loading=_attr(tag, "loading"),
                in_link=bool(flags & _IN_ANCHOR),
            )
        )
    return tuple(images)


def _json_ld(tags: Tags) -> tuple[JsonLdBlock, ...]:
    blocks = []
    for tag, _ in tags:
        if tag.name != "script" or not _JSON_LD_TYPE.match(_attr(tag, "type") or ""):
            continue
        raw = tag.string if tag.string is not None else tag.get_text()
        blocks.append(parse_json_ld(str(raw)))
    return tuple(blocks)


def parse_json_ld(raw: str) -> JsonLdBlock:
    """Parse one JSON-LD block, recording syntax errors instead of raising."""
    stored = raw.strip()[:_MAX_RAW_JSON_LD]
    text = raw.strip()
    if not text:
        return JsonLdBlock(raw=stored, error="empty JSON-LD block")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return JsonLdBlock(
            raw=stored, error=f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        )
    entities = _top_level_entities(data)
    if not entities:
        return JsonLdBlock(raw=stored, data=data, error="JSON-LD contains no JSON objects")

    types: list[str] = []
    for entity in entities:
        value = entity.get("@type")
        if isinstance(value, str):
            types.append(value)
        elif isinstance(value, list):
            types.extend(item for item in value if isinstance(item, str))

    roots = data if isinstance(data, list) else [data]
    missing_context = any(isinstance(r, dict) and "@context" not in r for r in roots)
    missing_type = any("@type" not in entity for entity in entities)
    return JsonLdBlock(
        raw=stored,
        data=data,
        types=tuple(dict.fromkeys(types)),
        missing_context=missing_context,
        missing_type=missing_type,
    )


def _top_level_entities(data: Any) -> list[dict[str, Any]]:
    """Top-level objects, expanding ``@graph`` containers."""
    roots = data if isinstance(data, list) else [data]
    entities: list[dict[str, Any]] = []
    for root in roots:
        if not isinstance(root, dict):
            continue
        graph = root.get("@graph")
        if isinstance(graph, list):
            entities.extend(item for item in graph if isinstance(item, dict))
            if "@type" in root:
                entities.append(root)
        else:
            entities.append(root)
    return entities


def _subresources(tags: Tags, base_url: str) -> tuple[str, ...]:
    values: list[str] = []
    for tag, _ in tags:
        attr = _SUBRESOURCE_ATTRS.get(tag.name)
        if attr and (value := _attr(tag, attr)):
            values.append(value)
        elif tag.name == "link" and "stylesheet" in _rel(tag) and (href := _attr(tag, "href")):
            values.append(href)
    resolved = (normalize_url(value, base_url) for value in values)
    return tuple(dict.fromkeys(url for url in resolved if url is not None))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _base_url(soup: BeautifulSoup, url: str) -> str:
    base = soup.find("base")
    href = _attr(base, "href") if isinstance(base, Tag) else None
    if href:
        resolved = normalize_url(href, url)
        if resolved:
            return resolved
    return url


def _has(tag: PageElement, attr: str) -> bool:
    return isinstance(tag, Tag) and tag.has_attr(attr)


def _attr(tag: Tag | None, name: str) -> str | None:
    """Attribute value as a string (multi-valued attributes are space-joined)."""
    if tag is None:
        return None
    value = tag.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value)


def _rel(tag: Tag | None) -> tuple[str, ...]:
    return tuple(token.lower() for token in (_attr(tag, "rel") or "").split())


def _accessible_name(tag: Tag) -> str:
    """Approximate accessible name for a link (text, aria-label, title, image alt)."""
    text = _bounded_text(tag)
    if text:
        return text
    for attr in ("aria-label", "title"):
        value = clean_text(_attr(tag, attr) or "")
        if value:
            return value
    for node in _bounded_descendants(tag):
        if isinstance(node, Tag) and node.name == "img":
            alt = clean_text(_attr(node, "alt") or "")
            if alt:
                return alt
    return ""
