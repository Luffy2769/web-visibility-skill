"""Resource limits under hostile input, over real sockets (independent audit #2).

Decompression bombs in every framing, hostile sitemap XML, and the robots.txt
wildcard ReDoS (N3). Memory is measured with ``tracemalloc`` and time with a
generous wall-clock bound, so the tests stay stable on slow CI runners while
still failing by orders of magnitude if a limit is removed.
"""

from __future__ import annotations

import gzip
import os
import subprocess
import sys
import tracemalloc
import zlib
from collections.abc import Callable

import pytest

from web_visibility.config import FetchConfig
from web_visibility.fetch import Fetcher
from web_visibility.robots import wildcard_match
from web_visibility.sitemap import SitemapParseError, parse_sitemap

from live_server import Handler, html_page, live_audit, live_server, send

pytestmark = pytest.mark.integration

LIMIT = 2_000_000
_PAGE = b"<html><head><title>x</title></head><body>" + b"A" * 120_000_000 + b"</body></html>"
_GZIP_BOMB = gzip.compress(_PAGE, compresslevel=9)  # ~120 KB on the wire


def _streamed(
    body: bytes, encoding: str | None, *, chunked: bool = False
) -> Callable[[Handler], None]:
    def respond(h: Handler) -> None:
        head = ["HTTP/1.1 200 OK", "Content-Type: text/html", "Connection: close"]
        head += [f"Content-Encoding: {encoding}"] if encoding else []
        head.append("Transfer-Encoding: chunked" if chunked else f"Content-Length: {len(body)}")
        h.wfile.write(("\r\n".join(head) + "\r\n\r\n").encode())
        view = memoryview(body)
        for i in range(0, len(body), 16_384):
            part = view[i : i + 16_384]
            h.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n" if chunked else part)
        if chunked:
            h.wfile.write(b"0\r\n\r\n")
        h.close_connection = True

    return respond


def _deflate(data: bytes, *, raw: bool) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15 if raw else 15)
    return compressor.compress(data) + compressor.flush()


BOMBS = {
    "gzip": (_GZIP_BOMB, "gzip", False),
    "gzip-chunked": (_GZIP_BOMB, "gzip", True),
    "x-gzip": (_GZIP_BOMB, "x-gzip", False),
    "deflate-zlib": (_deflate(_PAGE, raw=False), "deflate", False),
    "deflate-raw": (_deflate(_PAGE, raw=True), "deflate", False),
    "concatenated-members": (gzip.compress(b"B" * 1_000_000) * 150, "gzip", False),
    "identity-50MB": (b"<html>" + b"C" * 50_000_000, None, False),
}


@pytest.mark.parametrize("name", list(BOMBS))
def test_response_bodies_are_bounded_in_memory(name: str) -> None:
    body, encoding, chunked = BOMBS[name]
    with live_server() as srv:
        srv.route("/bomb", _streamed(body, encoding, chunked=chunked))
        srv.route("/via-redirect", lambda h: send(h, 302, headers={"Location": "/bomb"}))
        with Fetcher(FetchConfig(allow_private=True, delay=0, max_bytes=LIMIT, timeout=20)) as f:
            for path in ("/bomb", "/via-redirect"):
                tracemalloc.start()
                result = f.fetch(srv.base + path)
                peak = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
                assert result.truncated and len(result.body) == LIMIT, path
                assert peak < 8 * LIMIT, f"{path}: peak {peak:,} bytes"


@pytest.mark.parametrize(("encoding", "body"), [("br", b"\x1b\x00\x00"), ("gzip, gzip", b"xx")])
def test_unrequested_or_stacked_encodings_are_errors(encoding: str, body: bytes) -> None:
    with live_server() as srv:
        srv.page("/", body, headers={"Content-Encoding": encoding})
        with Fetcher(FetchConfig(allow_private=True, delay=0)) as f:
            assert f.fetch(srv.base + "/").error_kind == "unsupported-encoding"


@pytest.mark.parametrize(
    "headers",
    [[("X-Big", "a" * 1_000_000)], [(f"X-H{i}", "b" * 1000) for i in range(2000)]],
    ids=["one-1MB-header", "2000-headers"],
)
def test_oversized_response_headers_are_refused(headers: list[tuple[str, str]]) -> None:
    with live_server() as srv:
        srv.page("/", b"<html></html>", headers=headers)  # type: ignore[arg-type]
        with Fetcher(FetchConfig(allow_private=True, delay=0)) as f:
            result = f.fetch(srv.base + "/")
    assert result.status_code is None and result.error_kind == "http"


# --------------------------------------------------------------------------- #
# Sitemap XML (N5)
# --------------------------------------------------------------------------- #

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def test_deeply_nested_sitemap_is_rejected_with_bounded_memory() -> None:
    """Was 563 MB peak for a 7 MB document (no DTD needed); projects to ~4 GB at 50 MB."""
    data = ("<urlset>" + "<a>" * 2_000_000 + "</a>" * 2_000_000 + "</urlset>").encode()
    tracemalloc.start()
    with pytest.raises(SitemapParseError, match="nested deeper"):
        parse_sitemap(data)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 20_000_000


def test_large_legitimate_sitemap_is_streamed() -> None:
    entries = "".join(f"<url><loc>https://example.com/p/{i}</loc></url>" for i in range(200_000))
    data = f"<urlset {NS}>{entries}</urlset>".encode()  # ~10 MB
    tracemalloc.start()
    document = parse_sitemap(data)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert len(document.locs) == 200_000
    assert peak < 3 * len(data)  # was ~6x the document size before streaming


def test_realistic_extension_nesting_is_accepted() -> None:
    data = (
        f'<urlset {NS} xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        "<url><loc>https://example.com/a</loc><news:news><news:publication><news:name>N"
        "</news:name></news:publication></news:news></url></urlset>"
    ).encode()
    assert parse_sitemap(data).locs == ("https://example.com/a",)


def test_hostile_sitemap_through_the_crawler_does_not_stop_the_audit() -> None:
    with live_server() as srv:
        srv.page(
            "/robots.txt", "User-agent: *\nSitemap: {BASE}/deep.xml\n", content_type="text/plain"
        )
        srv.page("/deep.xml", "<urlset>" + "<a>" * 200_000 + "</a>" * 200_000 + "</urlset>",
                 content_type="application/xml")  # fmt: skip
        srv.page("/", html_page("Home page title"))
        report = live_audit(srv)
    assert report.pages_audited == 1
    assert "sitemap-invalid" in {i.id for i in report.issues}


# --------------------------------------------------------------------------- #
# robots.txt wildcard ReDoS (N3)
# --------------------------------------------------------------------------- #


def _within(seconds: float, code: str) -> str:
    """Run ``code`` in a fresh interpreter and require it to finish in time.

    A subprocess, not a thread: a backtracking regex holds the GIL, so a hung
    match could not be interrupted (or even timed) from inside this process.
    """
    try:
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              timeout=seconds, env=os.environ.copy())  # fmt: skip
    except subprocess.TimeoutExpired:
        pytest.fail(f"did not finish within {seconds} s (exponential matching?)")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_hostile_wildcard_rule_is_evaluated_in_linear_time() -> None:
    """``/*a*a*a*a*a*a*a*a*b`` took 39 s on a 61-char path; more wildcards: hours."""
    rule = "/" + "*a" * 40 + "*b"
    code = f"""
from web_visibility.robots import RobotsTxt
rules = RobotsTxt.parse("User-agent: *\\nDisallow: {rule}\\n")
print(all(rules.is_allowed("/" + "a" * n, "x") for n in (60, 600, 6000)))
"""
    assert _within(20, code) == "True"


def test_hostile_robots_and_crafted_link_do_not_hang_a_real_crawl() -> None:
    with live_server() as srv:
        robots_txt = "User-agent: *\nDisallow: /" + "*a" * 10 + "*b\n"
        srv.page("/robots.txt", robots_txt, content_type="text/plain")
        crafted = "/" + "a" * 200
        srv.page("/", html_page("Home page title", links=(crafted,)))
        srv.page(crafted, html_page("Crafted page title"))
        code = f"""
from web_visibility.audit import run_audit
from web_visibility.config import CrawlConfig, FetchConfig
config = CrawlConfig(fetch=FetchConfig(allow_private=True, delay=0, timeout=5))
print(run_audit({srv.base + "/"!r}, config).pages_audited)
"""
        assert _within(60, code) == "2"


def _regex_oracle(pattern: str, target: str) -> bool:
    """The previous (backtracking) implementation: correct, but exponential."""
    import re

    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.match(regex + ("$" if anchored else ""), target) is not None


def test_linear_matcher_agrees_with_the_regex_semantics() -> None:
    import itertools
    import random

    rng = random.Random(9309)
    alphabet = "ab/*"
    patterns = {"".join(p) for n in range(1, 5) for p in itertools.product(alphabet, repeat=n)}
    patterns |= {p + "$" for p in list(patterns)}
    targets = ["".join(rng.choice("ab/$") for _ in range(rng.randint(0, 8))) for _ in range(150)]
    for pattern in patterns:
        for target in targets:
            assert wildcard_match(pattern, target) == _regex_oracle(pattern, target), (
                pattern,
                target,
            )


def test_raw_compressed_input_is_capped_even_when_it_inflates_to_nothing() -> None:
    """Thousands of empty gzip members: tiny output, unbounded download without a raw cap."""
    body = gzip.compress(b"") * 20_000  # ~400 KB raw, 0 bytes decompressed
    with live_server() as srv:
        srv.route("/", _streamed(body, "gzip"))
        with Fetcher(FetchConfig(allow_private=True, delay=0, max_bytes=100_000)) as f:
            result = f.fetch(srv.base + "/")
    assert result.truncated
