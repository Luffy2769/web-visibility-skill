"""Polite, bounded HTTP fetching.

The fetcher is the only component that touches the network. It enforces:

* the SSRF guard on the initial URL **and every redirect hop**, connecting to
  the validated IP address (no second DNS lookup, see :mod:`web_visibility.safety`),
* a redirect limit with loop detection (redirects are followed manually),
* a **decompressed** body limit: raw bytes are read with ``iter_raw()`` and
  decompressed here with bounded output, so a small gzip response cannot expand
  past the limit in memory,
* per-read timeouts plus a total body deadline,
* a minimum delay between requests (raised to a robots.txt ``Crawl-delay``),
* one retry honouring ``Retry-After`` on 429/503 (if the wait is acceptable),
* an honest User-Agent (it never impersonates a search-engine crawler).

Network failures are returned as data (``FetchResult.error``), not raised.
``sleep`` and ``clock`` are injectable so tests never wait in real time.
"""

from __future__ import annotations

import logging
import ssl
import time
import zlib
from collections.abc import Callable
from dataclasses import replace
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from web_visibility.config import ROBOTS_USER_AGENT_TOKEN, USER_AGENT, FetchConfig
from web_visibility.models import FetchResult, RedirectHop
from web_visibility.safety import HostResolutionError, UnsafeURLError, resolve_target

logger = logging.getLogger(__name__)

__all__ = ["ROBOTS_USER_AGENT_TOKEN", "USER_AGENT", "FetchConfig", "Fetcher"]

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RETRY_AFTER_STATUSES = frozenset({429, 503})
# Total time allowed for a response body, as a multiple of the per-read timeout.
BODY_DEADLINE_FACTOR = 3
TRANSIENT_ERRORS = frozenset({"timeout", "connection"})
_REDACTED_HEADERS = frozenset({"set-cookie", "set-cookie2"})
# Headers whose repeated values must stay distinguishable (see Page.directives_for).
_NEWLINE_JOINED_HEADERS = frozenset({"x-robots-tag"})

Sleep = Callable[[float], None]
Clock = Callable[[], float]


class _BodyDeadlineExceededError(Exception):
    """Internal: the body trickled in slower than the total deadline allows."""


class _UnsupportedEncodingError(Exception):
    """Internal: the server used a Content-Encoding we did not ask for."""


class Fetcher:
    """Synchronous HTTP fetcher. Use as a context manager to close connections."""

    def __init__(
        self,
        config: FetchConfig | None = None,
        client: httpx.Client | None = None,
        *,
        sleep: Sleep = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.config = config or FetchConfig()
        # trust_env=False: no implicit proxies from the environment, so requests go
        # exactly where the SSRF guard validated them.
        self._client = client or httpx.Client(
            timeout=self.config.timeout, follow_redirects=False, trust_env=False
        )
        self._client.headers["User-Agent"] = self.config.user_agent
        self._sleep = sleep
        self._clock = clock
        self._min_delay = self.config.delay
        self._last_request_at: float | None = None
        self.request_count = 0
        self.slept_seconds = 0.0

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def set_min_delay(self, seconds: float) -> None:
        """Raise the delay between requests (e.g. to honour ``Crawl-delay``)."""
        self._min_delay = max(self.config.delay, seconds)

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        read_body: bool = True,
        max_bytes: int | None = None,
        accept: str = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        retry_transient: bool = False,
    ) -> FetchResult:
        """Fetch ``url``, following redirects safely. Never raises for network errors.

        A 429/503 carrying an acceptable ``Retry-After`` is retried once after the
        requested wait. With ``retry_transient``, a timeout or connection failure is
        retried ``config.transient_retries`` times after a 1 s pause.
        """
        transient_left = self.config.transient_retries if retry_transient else 0
        retried_after = False
        retries = 0
        while True:
            result = self._fetch_once(url, method, read_body, max_bytes, accept)
            if result.error_kind in TRANSIENT_ERRORS and transient_left > 0:
                transient_left -= 1
                retries += 1
                self._pause(1.0)
                continue
            wait = None if retried_after else _retry_after(result)
            if wait is not None and wait <= self.config.max_retry_wait:
                retried_after = True
                retries += 1
                logger.info("HTTP %s from %s; retrying after %.1fs", result.status_code, url, wait)
                self._pause(wait)
                continue
            return replace(result, retries=retries) if retries else result

    # ------------------------------------------------------------------ #

    def _fetch_once(
        self, url: str, method: str, read_body: bool, max_bytes: int | None, accept: str
    ) -> FetchResult:
        limit = max_bytes or self.config.max_bytes
        chain: list[RedirectHop] = []
        seen = {url}
        current = url

        for _ in range(self.config.max_redirects + 1):
            try:
                target = resolve_target(
                    current, allow_private=self.config.allow_private, timeout=self.config.timeout
                )
            except UnsafeURLError as exc:
                logger.warning("blocked unsafe URL %s: %s", current, exc)
                return _error(url, current, chain, "blocked", str(exc))
            except HostResolutionError as exc:
                kind = "timeout" if "timed out" in str(exc) else "dns"
                return _error(url, current, chain, kind, str(exc))

            self._throttle()
            request_url, headers, extensions = _pinned_request(current, target.address)
            headers["Accept"] = accept
            headers["Accept-Encoding"] = "gzip, deflate"
            try:
                with self._client.stream(
                    method, request_url, headers=headers, extensions=extensions
                ) as response:
                    status = response.status_code
                    location = response.headers.get("location")
                    if status in REDIRECT_STATUSES and location:
                        chain.append(RedirectHop(current, status))
                        next_url = _strip_fragment(urljoin(current, location.strip()))
                        if next_url in seen:
                            message = f"redirect loop detected at {next_url}"
                            return _error(url, next_url, chain, "redirect-loop", message)
                        seen.add(next_url)
                        if status == 303 and method != "HEAD":
                            method = "GET"
                        current = next_url
                        continue

                    body, truncated = b"", False
                    if read_body:
                        deadline = self._clock() + self.config.timeout * BODY_DEADLINE_FACTOR
                        body, truncated = self._read_body(response, limit, deadline)
                    return FetchResult(
                        requested_url=url,
                        final_url=current,
                        status_code=status,
                        headers=_collect_headers(response.headers),
                        body=body,
                        redirect_chain=tuple(chain),
                        truncated=truncated,
                    )
            except httpx.TimeoutException as exc:
                return _error(url, current, chain, "timeout", f"request timed out: {exc!r}")
            except _BodyDeadlineExceededError:
                seconds = self.config.timeout * BODY_DEADLINE_FACTOR
                message = f"response body not received within {seconds:g}s"
                return _error(url, current, chain, "timeout", message)
            except _UnsupportedEncodingError as exc:
                return _error(url, current, chain, "unsupported-encoding", str(exc))
            except httpx.ConnectError as exc:
                kind = "ssl" if _is_ssl_error(exc) else "connection"
                return _error(url, current, chain, kind, f"{kind} error: {exc}")
            except (httpx.HTTPError, httpx.InvalidURL, zlib.error) as exc:
                return _error(url, current, chain, "http", f"HTTP error: {exc}")

        message = f"more than {self.config.max_redirects} redirects"
        return _error(url, current, chain, "too-many-redirects", message)

    def _read_body(
        self, response: httpx.Response, limit: int, deadline: float
    ) -> tuple[bytes, bool]:
        """Read raw bytes and decompress with bounded output: ``(body, truncated)``.

        Memory use is bounded by ``limit`` plus one raw chunk, whatever the
        compression ratio, because zlib is never asked for more than ``limit + 1``
        output bytes. Raw (compressed) input is capped at ``limit`` bytes as well.
        """
        if response.is_stream_consumed:
            # Only happens with pre-built in-memory responses (e.g. httpx.MockTransport
            # given ``content=``); a network response is always streamed.
            content = response.content
            return content[:limit], len(content) > limit
        decoder = _BoundedDecoder(response.headers.get("content-encoding", ""))
        out = bytearray()
        raw_total = 0
        for raw in response.iter_raw():  # chunks as received: the deadline is checked often
            if self._clock() > deadline:
                raise _BodyDeadlineExceededError
            raw_total += len(raw)
            out += decoder.feed(raw, limit - len(out) + 1)
            if len(out) > limit or raw_total > limit:
                return bytes(out[:limit]), True
        out += decoder.flush(limit - len(out) + 1)
        if len(out) > limit:
            return bytes(out[:limit]), True
        return bytes(out), False

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_request_at is not None and self._min_delay > 0:
            wait = self._min_delay - (now - self._last_request_at)
            if wait > 0:
                self._pause(wait)
        self._last_request_at = self._clock()
        self.request_count += 1

    def _pause(self, seconds: float) -> None:
        self.slept_seconds += seconds
        self._sleep(seconds)


# --------------------------------------------------------------------------- #
# Bounded decompression
# --------------------------------------------------------------------------- #


class _BoundedDecoder:
    """Incremental gzip/deflate decoder that never produces more than asked for."""

    def __init__(self, content_encoding: str) -> None:
        codings = [c.strip().lower() for c in content_encoding.split(",") if c.strip()]
        codings = [c for c in codings if c != "identity"]
        if len(codings) > 1:
            raise _UnsupportedEncodingError(f"stacked content encodings: {content_encoding}")
        self.coding = codings[0] if codings else None
        if self.coding not in (None, "gzip", "x-gzip", "deflate"):
            raise _UnsupportedEncodingError(f"unsupported Content-Encoding: {self.coding}")
        self._d: Any = None

    def _decompressor(self, first: bytes) -> Any:
        if self.coding in ("gzip", "x-gzip"):
            return zlib.decompressobj(16 + zlib.MAX_WBITS)
        # "deflate" should be zlib-wrapped, but raw deflate is common in the wild.
        zlib_wrapped = (
            len(first) >= 2 and (first[0] & 0x0F) == 8 and int.from_bytes(first[:2]) % 31 == 0
        )
        return zlib.decompressobj(zlib.MAX_WBITS if zlib_wrapped else -zlib.MAX_WBITS)

    def feed(self, data: bytes, budget: int) -> bytes:
        """Decompress ``data``; return at most ``budget`` bytes (``budget >= 1``)."""
        if self.coding is None:
            return data[:budget]
        if self._d is None:
            self._d = self._decompressor(data)
        out = bytearray()
        while data and len(out) < budget:
            out += self._d.decompress(data, budget - len(out))
            data = self._d.unconsumed_tail
            if self._d.eof and self._d.unused_data:  # concatenated gzip members
                data = self._d.unused_data + data
                self._d = self._decompressor(data)
        return bytes(out)

    def flush(self, budget: int) -> bytes:
        if self.coding is None or self._d is None:
            return b""
        return bytes(self._d.flush(budget))[:budget]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _pinned_request(url: str, address: str | None) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Rewrite ``url`` to connect to ``address`` while presenting the original host.

    The ``Host`` header and (for HTTPS) the TLS server name and certificate check
    use the original hostname, so virtual hosting and certificate validation work.
    """
    if address is None:
        return url, {}, {}
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host == address:
        return url, {}, {}
    ip_netloc = f"[{address}]" if ":" in address else address
    if parts.port is not None:
        ip_netloc += f":{parts.port}"
    pinned = urlunsplit((parts.scheme, ip_netloc, parts.path, parts.query, ""))
    extensions: dict[str, Any] = {"sni_hostname": host} if parts.scheme == "https" else {}
    return pinned, {"Host": parts.netloc.rsplit("@", 1)[-1]}, extensions


def _retry_after(result: FetchResult) -> float | None:
    """Seconds requested by ``Retry-After`` on a 429/503, if parseable."""
    if result.status_code not in RETRY_AFTER_STATUSES:
        return None
    value = result.headers.get("retry-after", "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def _error(
    url: str, current: str, chain: list[RedirectHop], kind: str, message: str
) -> FetchResult:
    logger.debug("fetch failed for %s (%s): %s", url, kind, message)
    return FetchResult(
        requested_url=url,
        final_url=current,
        status_code=None,
        headers={},
        redirect_chain=tuple(chain),
        error=message,
        error_kind=kind,
    )


def _collect_headers(headers: httpx.Headers) -> dict[str, str]:
    collected: dict[str, str] = {}
    for name in dict.fromkeys(key.lower() for key in headers):
        if name in _REDACTED_HEADERS:
            collected[name] = "[redacted]"
            continue
        separator = "\n" if name in _NEWLINE_JOINED_HEADERS else ", "
        collected[name] = separator.join(headers.get_list(name))
    return collected


def _strip_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _is_ssl_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    return "ssl" in str(exc).lower() or "certificate" in str(exc).lower()
