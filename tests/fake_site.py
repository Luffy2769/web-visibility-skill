"""An in-memory website served through httpx.MockTransport, for end-to-end tests.

Bodies are streamed as raw bytes (like a real network response), so the
fetcher's raw-read / bounded-decompression path is exercised. Routes may be
host-specific (``"www.example.com/path"``) or path-only (``"/path"``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx

from web_visibility.audit import AuditReport, run_audit
from web_visibility.config import CrawlConfig, FetchConfig
from web_visibility.fetch import Fetcher
from web_visibility.models import Issue

HTML = {"content-type": "text/html; charset=utf-8"}
TEXT = {"content-type": "text/plain"}
XML = {"content-type": "application/xml"}

Responder = Callable[[httpx.Request], httpx.Response]


class RawStream(httpx.SyncByteStream):
    def __init__(self, data: bytes, chunk: int = 65536) -> None:
        self.data = data
        self.chunk = chunk

    def __iter__(self) -> Iterator[bytes]:
        for i in range(0, len(self.data), self.chunk):
            yield self.data[i : i + self.chunk]


@dataclass
class Route:
    status: int = 200
    body: bytes | str = b""
    headers: dict[str, str] = field(default_factory=lambda: dict(HTML))

    def response(self) -> httpx.Response:
        data = self.body.encode() if isinstance(self.body, str) else self.body
        return httpx.Response(self.status, headers=self.headers, stream=RawStream(data))


def page(title: str = "A descriptive page title", *, links: tuple[str, ...] = (),
         head: str = "", body: str = "", canonical: str | None = None) -> Route:  # fmt: skip
    """A well-formed HTML page (passes metadata checks unless overridden)."""
    canonical_tag = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    anchors = "".join(f'<a href="{href}">Link to {href}</a>' for href in links)
    text = "<p>" + "Plenty of real server-rendered content here. " * 6 + "</p>"
    html = (
        f"<!doctype html><html lang='en'><head><title>{title}</title>"
        f'<meta name="description" content="Description of {title}">{canonical_tag}{head}</head>'
        f"<body><h1>{title}</h1>{text}{anchors}{body}</body></html>"
    )
    return Route(200, html)


class FakeSite:
    def __init__(self, routes: dict[str, Route | Responder]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host_key = f"{request.url.host}{request.url.path}"
        route = self.routes.get(host_key) or self.routes.get(request.url.path)
        if route is None:
            return Route(404, "<h1>Not found</h1>").response()
        return route(request) if callable(route) else route.response()

    def paths(self, method: str = "GET") -> list[str]:
        return [r.url.path for r in self.requests if r.method == method]


def audit(
    site: FakeSite,
    start: str = "https://example.com/",
    *,
    fetch: dict[str, object] | None = None,
    sleeps: list[float] | None = None,
    **crawl: object,
) -> AuditReport:
    """Run the real audit pipeline against ``site`` (no network, no real sleeping)."""
    fetch_config = FetchConfig(delay=0, allow_private=True, **(fetch or {}))  # type: ignore[arg-type]
    record = sleeps if sleeps is not None else []
    fetcher = Fetcher(
        fetch_config,
        client=httpx.Client(transport=httpx.MockTransport(site), follow_redirects=False),
        sleep=record.append,
    )
    config = CrawlConfig(fetch=fetch_config, **crawl)  # type: ignore[arg-type]
    return run_audit(start, config, fetcher=fetcher)


def issue_ids(report: AuditReport) -> set[str]:
    return {i.id for i in report.issues}


def find(report: AuditReport, rule_id: str) -> list[Issue]:
    return [i for i in report.issues if i.id == rule_id]


def one(report: AuditReport, rule_id: str) -> Issue:
    found = find(report, rule_id)
    assert len(found) == 1, f"expected exactly one {rule_id}, got {[i.id for i in report.issues]}"
    return found[0]
