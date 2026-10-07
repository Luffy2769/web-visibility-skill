"""XML sitemap parsing (sitemaps.org protocol).

Supports ``<urlset>`` and ``<sitemapindex>`` documents, gzip-compressed
sitemaps, and the plain-text format (one URL per line).

XML safety: documents are parsed with ``defusedxml`` configured to reject any
DTD (``<!DOCTYPE``), entity declaration or external reference, wherever it
appears in the document - not just near the start. Sitemaps never need any of
these. Combined with the byte limits applied before parsing (50 MB, the
protocol maximum, for both the download and gzip inflation), this rules out
entity-expansion ("billion laughs") and external-entity (XXE) attacks.

Memory: the document is *streamed* (``iterparse``). Element nesting deeper than
``MAX_XML_DEPTH`` is rejected as soon as it is seen (hostile nesting needs no DTD
and otherwise costs ~80x its size in memory), and each ``<url>``/``<sitemap>``
entry is released once its ``<loc>`` is read, so memory does not grow with the
number of entries.
"""

from __future__ import annotations

import io
import zlib
from dataclasses import dataclass
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import iterparse

from web_visibility.models.sitemap import SitemapKind

MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # protocol limit for an uncompressed sitemap
#: Sitemaps need about 4 levels (urlset > url > news:news > news:name); 32 is generous.
MAX_XML_DEPTH = 32
_GZIP_MAGIC = b"\x1f\x8b"
_HTML_STARTS = (b"<!doctype html", b"<html")


class SitemapParseError(ValueError):
    """The document is not a parseable sitemap."""

    def __init__(self, message: str, *, looks_like_html: bool = False) -> None:
        super().__init__(message)
        self.looks_like_html = looks_like_html


@dataclass(frozen=True, slots=True)
class SitemapDocument:
    kind: SitemapKind
    locs: tuple[str, ...]
    """``<loc>`` values in document order (may contain duplicates or bad URLs)."""


def decompress_if_gzip(data: bytes, max_bytes: int = MAX_SITEMAP_BYTES) -> bytes:
    """Gunzip ``data`` if it is gzip-compressed, refusing to inflate past ``max_bytes``."""
    if not data.startswith(_GZIP_MAGIC):
        return data
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        inflated = decompressor.decompress(data, max_bytes)
    except zlib.error as exc:
        raise SitemapParseError(f"invalid gzip data: {exc}") from exc
    if decompressor.unconsumed_tail:
        raise SitemapParseError(f"decompressed sitemap exceeds {max_bytes} bytes")
    return inflated


def parse_sitemap(data: bytes, content_type: str | None = None) -> SitemapDocument:
    """Parse sitemap bytes. Raises :class:`SitemapParseError` on invalid input."""
    data = decompress_if_gzip(data)
    stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not stripped:
        raise SitemapParseError("sitemap is empty")
    if content_type == "text/html" or stripped[:64].lower().startswith(_HTML_STARTS):
        raise SitemapParseError("response is an HTML page, not a sitemap", looks_like_html=True)
    if not stripped.startswith(b"<"):
        if content_type == "text/plain" or stripped[:8].lower().startswith(b"http"):
            return _parse_text(stripped)
        raise SitemapParseError("content is neither XML nor a plain-text URL list")

    try:
        return _parse_xml(stripped)
    except DefusedXmlException as exc:
        raise SitemapParseError(f"unsafe XML rejected ({type(exc).__name__})") from exc
    except ParseError as exc:
        raise SitemapParseError(f"invalid XML: {exc}") from exc


def _parse_xml(data: bytes) -> SitemapDocument:
    """Stream the document, collecting each entry's ``<loc>`` and discarding the entry."""
    events = iterparse(
        io.BytesIO(data), events=("start", "end"),
        forbid_dtd=True, forbid_entities=True, forbid_external=True,
    )  # fmt: skip
    root: Element | None = None
    kind: SitemapKind = "urlset"
    entry_name = "url"
    locs: list[str] = []
    depth = 0
    for event, element in events:
        if event == "start":
            depth += 1
            if depth > MAX_XML_DEPTH:
                raise SitemapParseError(f"XML nested deeper than {MAX_XML_DEPTH} levels")
            if root is None:
                root = element
                name = _local_name(element.tag)
                if name not in ("urlset", "sitemapindex"):
                    raise SitemapParseError(
                        f"unexpected root element <{name}> (expected <urlset> or <sitemapindex>)",
                        looks_like_html=name == "html",
                    )
                kind = "urlset" if name == "urlset" else "sitemapindex"
                entry_name = "url" if kind == "urlset" else "sitemap"
            continue
        depth -= 1
        if depth == 1 and root is not None:  # an entry (or other child of the root) ended
            loc = _loc_of(element) if _local_name(element.tag) == entry_name else None
            if loc is not None:
                locs.append(loc)
            root.clear()  # release the entry and anything before it
    return SitemapDocument(kind, tuple(locs))


def _loc_of(entry: Element) -> str | None:
    for child in entry:
        if _local_name(child.tag) == "loc":
            return (child.text or "").strip()
    return None


def _parse_text(data: bytes) -> SitemapDocument:
    text = data.decode("utf-8", errors="replace")
    return SitemapDocument(
        "text", tuple(line.strip() for line in text.splitlines() if line.strip())
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
