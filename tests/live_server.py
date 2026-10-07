"""A scriptable HTTP server on a real local socket, for adversarial regression tests.

``fake_site.FakeSite`` drives the pipeline through ``httpx.MockTransport``; this
server exercises what a mock cannot: real sockets, h11 parsing, chunked and
truncated streams, connection drops, raw header bytes. Routes are plain
functions of the request handler, so a test can send exactly the bytes it needs.

Every request is logged, so tests can assert what the auditor did *not* fetch.
"""

from __future__ import annotations

import contextlib
import socket
import socketserver
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from web_visibility.audit import AuditReport, run_audit
from web_visibility.config import CrawlConfig, FetchConfig

Handler = BaseHTTPRequestHandler
Respond = Callable[[Handler], None]


class _QuickServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn(): a reverse DNS lookup that can
        # stall for seconds (e.g. for 127.0.0.2 on Windows). The name is unused here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", int(self.server_address[1])


class LiveServer:
    """Threaded HTTP/1.1 server bound to ``host`` on a free port."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.routes: dict[str, Respond] = {}
        self.default: Respond | None = None
        self.log: list[tuple[str, str, dict[str, str]]] = []
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def _dispatch(self) -> None:
                owner.log.append((self.command, self.path, dict(self.headers)))
                route = owner.routes.get(self.path) or owner.default
                try:
                    if route is None:
                        send(self, 404, b"<h1>Not found</h1>")
                    else:
                        route(self)
                except OSError:  # the auditor may drop a connection on purpose
                    self.close_connection = True

            do_GET = do_HEAD = _dispatch

        self._httpd = _QuickServer((host, 0), _Handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self.base = f"http://{host}:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def page(self, path: str, body: str | bytes, *, status: int = 200,
             content_type: str = "text/html; charset=utf-8",
             headers: dict[str, str] | None = None) -> None:  # fmt: skip
        data = body.replace("{BASE}", self.base).encode() if isinstance(body, str) else body
        self.routes[path] = lambda h: send(h, status, data, content_type=content_type,
                                           headers=headers)  # fmt: skip

    def route(self, path: str, respond: Respond) -> None:
        self.routes[path] = respond

    def hits(self, path: str | None = None) -> int:
        return sum(1 for _, p, _ in self.log if path is None or p == path)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@contextmanager
def live_server(host: str = "127.0.0.1") -> Iterator[LiveServer]:
    server = LiveServer(host)
    try:
        yield server
    finally:
        server.close()


def send(handler: Handler, status: int, body: bytes = b"", *,
         content_type: str | None = "text/html; charset=utf-8",
         headers: dict[str, str] | list[tuple[str, str]] | None = None) -> None:  # fmt: skip
    """A complete response with ``Content-Length`` (unless the headers set framing)."""
    handler.send_response(status)
    if content_type:
        handler.send_header("Content-Type", content_type)
    items = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
    names = {name.lower() for name, _ in items}
    for name, value in items:
        handler.send_header(name, value)
    if not names & {"content-length", "transfer-encoding"}:
        handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def send_raw(handler: Handler, data: bytes) -> None:
    """Write ``data`` verbatim (status line included), then close the connection."""
    handler.wfile.write(data)
    handler.wfile.flush()
    handler.close_connection = True
    with contextlib.suppress(OSError):
        handler.connection.shutdown(socket.SHUT_WR)


def html_page(title: str = "A descriptive page title", *, links: tuple[str, ...] = (),
              head: str = "", body: str = "") -> str:  # fmt: skip
    """A well-formed page that passes the metadata checks unless overridden."""
    anchors = "".join(f'<a href="{href}">Link to {href}</a>' for href in links)
    text = "<p>" + "Plenty of real server-rendered content here. " * 6 + "</p>"
    return (
        f"<!doctype html><html lang='en'><head><title>{title}</title>"
        f'<meta name="description" content="Description of {title}">{head}</head>'
        f"<body><h1>{title}</h1>{text}{anchors}{body}</body></html>"
    )


def live_audit(server: LiveServer, path: str = "/", *, fetch: dict[str, object] | None = None,
               **crawl: object) -> AuditReport:  # fmt: skip
    """Run the real pipeline against ``server`` over real sockets (no sleeping)."""
    options: dict[str, object] = {"delay": 0.0, "allow_private": True, "timeout": 5.0}
    options.update(fetch or {})
    fetch_config = FetchConfig(**options)  # type: ignore[arg-type]
    config = CrawlConfig(fetch=fetch_config, **crawl)  # type: ignore[arg-type]
    return run_audit(server.base + path, config)
