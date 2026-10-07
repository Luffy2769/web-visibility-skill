"""SSRF guard over real sockets (independent audit #2 probe suite, made permanent).

127.0.0.1 plays a *public* site (its address is classified as public by a patched
classifier); 127.0.0.2 plays an internal service and keeps its real (loopback)
classification. Any request reaching the internal server is a bypass.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

import pytest

import web_visibility.safety as safety
from web_visibility.audit import run_audit
from web_visibility.config import CrawlConfig, FetchConfig
from web_visibility.fetch import Fetcher
from web_visibility.safety import HostResolutionError, UnsafeURLError, resolve_target

from live_server import LiveServer, live_server, send

pytestmark = pytest.mark.integration

PUBLIC = ipaddress.ip_address("127.0.0.1")


@pytest.fixture
def internal() -> Iterator[LiveServer]:
    try:
        server = LiveServer("127.0.0.2")
    except OSError:  # pragma: no cover - platforms without the whole 127/8 on loopback
        pytest.skip("127.0.0.2 is not bindable here")
    server.default = lambda h: send(h, 200, b"<title>INTERNAL SECRET</title>")
    yield server
    server.close()


@pytest.fixture
def public_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    real = safety.non_public_reason
    monkeypatch.setattr(safety, "non_public_reason", lambda a: None if a == PUBLIC else real(a))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/", "http://localhost/", "http://LOCALHOST./", "http://10.1.2.3/",
        "http://172.16.0.1/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/", "http://0.0.0.0/", "http://[::1]/", "http://[::]/",
        "http://[::ffff:127.0.0.1]/", "http://[::ffff:a9fe:a9fe]/", "http://[fc00::1]/",
        "http://[fe80::1]/", "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/",
        "http://user:pw@example.com/", "ftp://127.0.0.1/",
    ],
)  # fmt: skip
def test_non_public_literal_forms_are_refused(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        resolve_target(url, timeout=5)


@pytest.mark.parametrize("host", ["2130706433", "0x7f000001", "017700000001", "127.1"])
def test_numeric_host_spellings_never_reach_loopback(host: str) -> None:
    """Some resolvers expand these to 127.0.0.1 (refused); others fail (no request)."""
    with pytest.raises((UnsafeURLError, HostResolutionError)):
        resolve_target(f"http://{host}/", timeout=5)


@pytest.mark.usefixtures("public_loopback")
@pytest.mark.parametrize(
    "location",
    [
        "{I}/admin", "http://localhost:{P}/x", "http://169.254.169.254/latest/meta-data/",
        "http://u:p@127.0.0.2:{P}/", "http://[::ffff:127.0.0.2]:{P}/",
    ],
)  # fmt: skip
def test_redirect_hops_to_internal_targets_are_blocked(internal: LiveServer, location: str) -> None:
    with live_server() as pub:
        target = location.replace("{I}", internal.base).replace("{P}", str(internal.port))
        pub.route("/r1", lambda h: send(h, 302, headers={"Location": "/r2"}))
        pub.route("/r2", lambda h: send(h, 302, headers={"Location": target}))
        with Fetcher(FetchConfig(delay=0, timeout=5)) as fetcher:
            result = fetcher.fetch(pub.base + "/r1")
    assert result.error_kind == "blocked"
    assert internal.hits() == 0


@pytest.mark.usefixtures("public_loopback")
def test_dns_rebinding_cannot_redirect_the_connection(
    internal: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = socket.getaddrinfo
    answers = iter(["127.0.0.1"] + ["127.0.0.2"] * 50)

    def rebinding(host: str, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host == "rebind.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    with live_server() as pub:
        pub.page("/", "<title>public</title>")
        with Fetcher(FetchConfig(delay=0, timeout=5)) as fetcher:
            result = fetcher.fetch(f"http://rebind.test:{pub.port}/")
        assert result.status_code == 200
        assert pub.log[-1][2]["Host"] == f"rebind.test:{pub.port}"
    assert internal.hits() == 0


@pytest.mark.usefixtures("public_loopback")
def test_crawler_entry_points_never_reach_internal_targets(internal: LiveServer) -> None:
    """robots Sitemap:, sitemap index children, links, images and canonicals."""
    with live_server() as pub:
        i = internal.base
        pub.page("/robots.txt", f"User-agent: *\nAllow: /\nSitemap: {i}/sm.xml\nSitemap: {{BASE}}/index.xml\n",
                 content_type="text/plain")  # fmt: skip
        pub.page("/index.xml", '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 f"<sitemap><loc>{i}/child.xml</loc></sitemap></sitemapindex>",
                 content_type="application/xml")  # fmt: skip
        pub.page("/", f'<html><head><title>Public home page</title><link rel="canonical" href="{i}/c">'
                 f'</head><body><h1>x</h1><a href="{i}/link">a</a><a href="http://169.254.169.254/">b</a>'
                 f'<img src="{i}/img.png" alt="i"></body></html>')  # fmt: skip
        config = CrawlConfig(check_external=True, fetch=FetchConfig(delay=0, timeout=5))
        report = run_audit(pub.base + "/", config)
    assert internal.hits() == 0
    blocked = {u for u, r in report.crawl.link_checks.items() if r.error_kind == "blocked"}
    assert f"{internal.base}/link" in blocked and "http://169.254.169.254/" in blocked
    # Blocked targets are unverifiable: never "checked", never "broken".
    assert "broken-external-link" not in {i.id for i in report.issues}
