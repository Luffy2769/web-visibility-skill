import gzip

import pytest

from web_visibility.sitemap import SitemapParseError, decompress_if_gzip, parse_sitemap

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc> https://example.com/ </loc><lastmod>2025-01-01</lastmod></url>
  <url><loc>https://example.com/about</loc></url>
  <url><priority>0.5</priority></url>
</urlset>"""

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap-posts.xml</loc></sitemap>
</sitemapindex>"""


def test_urlset_locs_are_extracted_and_trimmed() -> None:
    doc = parse_sitemap(URLSET)
    assert doc.kind == "urlset"
    assert doc.locs == ("https://example.com/", "https://example.com/about")


def test_sitemap_index() -> None:
    doc = parse_sitemap(INDEX)
    assert doc.kind == "sitemapindex"
    assert len(doc.locs) == 2


def test_gzip_sitemap() -> None:
    assert parse_sitemap(gzip.compress(URLSET)).locs == parse_sitemap(URLSET).locs


def test_gzip_bomb_is_refused() -> None:
    bomb = gzip.compress(b"<" + b"a" * 2_000_000)
    with pytest.raises(SitemapParseError, match="exceeds"):
        decompress_if_gzip(bomb, max_bytes=1000)


def test_plain_text_sitemap() -> None:
    doc = parse_sitemap(b"https://example.com/\n\nhttps://example.com/a\n", "text/plain")
    assert doc.kind == "text"
    assert doc.locs == ("https://example.com/", "https://example.com/a")


def test_utf8_bom_is_tolerated() -> None:
    assert parse_sitemap(b"\xef\xbb\xbf" + URLSET).kind == "urlset"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (b"<urlset><url><loc>x</loc></url>", "invalid XML"),
        (b"<html><body>Not found</body></html>", "HTML page"),
        (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset/>', "unsafe XML"),
        (b"just some words", "neither XML"),
    ],
)
def test_invalid_documents(data: bytes, message: str) -> None:
    with pytest.raises(SitemapParseError, match=message):
        parse_sitemap(data)


def test_doctype_hidden_after_a_large_comment_is_rejected() -> None:
    data = b"<!--" + b"x" * 50_000 + b'--><!DOCTYPE urlset [<!ENTITY e "boom">]><urlset/>'
    with pytest.raises(SitemapParseError, match="unsafe XML"):
        parse_sitemap(data)


def test_html_response_is_flagged() -> None:
    with pytest.raises(SitemapParseError) as excinfo:
        parse_sitemap(b"<!doctype html><html><body>app</body></html>", "text/html")
    assert excinfo.value.looks_like_html
