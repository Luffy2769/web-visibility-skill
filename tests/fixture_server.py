"""A tiny local HTTP server for the fixture website.

* serves files from ``tests/fixtures/site``,
* replaces ``{{BASE}}`` in text files with the server's base URL (sitemaps and
  canonicals need absolute URLs, but the port is chosen at runtime),
* answers a few synthetic routes (redirects) defined in ``REDIRECTS``.

Usable from tests (``serve_fixture()``) or manually::

    python tests/fixture_server.py   # prints the URL, Ctrl+C to stop
"""

from __future__ import annotations

import contextlib
import mimetypes
import threading
from collections.abc import Iterator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

SITE_ROOT = Path(__file__).parent / "fixtures" / "site"
REDIRECTS = {"/old-page": "/about.html"}
_TEXT_TYPES = {".html": "text/html; charset=utf-8", ".xml": "application/xml", ".txt": "text/plain"}


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "FixtureServer/1.0"

    def do_GET(self) -> None:
        self._respond(include_body=True)

    def do_HEAD(self) -> None:
        self._respond(include_body=False)

    def _respond(self, *, include_body: bool) -> None:
        path = urlsplit(self.path).path
        if path in REDIRECTS:
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", REDIRECTS[path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        file = (SITE_ROOT / (path.lstrip("/") or "index.html")).resolve()
        if file.is_dir():
            file = file / "index.html"
        if SITE_ROOT.resolve() not in file.parents or not file.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"<h1>Not found</h1>", "text/html", include_body)
            return

        body = file.read_bytes()
        content_type = _TEXT_TYPES.get(file.suffix)
        if content_type:
            base = f"http://{self.headers.get('Host')}"
            body = body.replace(b"{{BASE}}", base.encode())
        else:
            content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, body, content_type, include_body)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, include_body: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass  # keep test output quiet


@contextlib.contextmanager
def serve_fixture() -> Iterator[str]:
    """Run the fixture server on an ephemeral port; yields its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    with serve_fixture() as url:
        print(f"Fixture site running at {url}/  (Ctrl+C to stop)")
        with contextlib.suppress(KeyboardInterrupt):
            threading.Event().wait()
