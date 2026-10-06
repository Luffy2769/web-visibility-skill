from collections.abc import Callable

import httpx
import pytest

from web_visibility.fetch import USER_AGENT, FetchConfig, Fetcher

Handler = Callable[[httpx.Request], httpx.Response]


def make_fetcher(handler: Handler, **config: object) -> Fetcher:
    settings: dict[str, object] = {"delay": 0, "allow_private": True, **config}
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return Fetcher(FetchConfig(**settings), client=client)  # type: ignore[arg-type]


def test_success_records_status_headers_body_and_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8", "Set-Cookie": "session=secret"},
            content=b"<html></html>",
        )

    result = make_fetcher(handler).fetch("https://example.com/")
    assert result.ok and result.status_code == 200
    assert result.content_type == "text/html"
    assert result.charset == "utf-8"
    assert result.body == b"<html></html>"
    assert result.headers["set-cookie"] == "[redacted]"
    assert seen[0].headers["User-Agent"] == USER_AGENT
    assert "Googlebot" not in USER_AGENT


def test_redirects_are_followed_and_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/middle"})
        if request.url.path == "/middle":
            return httpx.Response(302, headers={"Location": "https://example.com/new#frag"})
        return httpx.Response(200, text="ok")

    result = make_fetcher(handler).fetch("https://example.com/old")
    assert result.final_url == "https://example.com/new"
    assert [(h.url, h.status_code) for h in result.redirect_chain] == [
        ("https://example.com/old", 301),
        ("https://example.com/middle", 302),
    ]


def test_redirect_loop_is_detected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        target = "/b" if request.url.path == "/a" else "/a"
        return httpx.Response(301, headers={"Location": target})

    result = make_fetcher(handler).fetch("https://example.com/a")
    assert result.error_kind == "redirect-loop"
    assert result.status_code is None


def test_redirect_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/") or 0)
        return httpx.Response(301, headers={"Location": f"/{n + 1}"})

    result = make_fetcher(handler, max_redirects=3).fetch("https://example.com/0")
    assert result.error_kind == "too-many-redirects"
    assert len(result.redirect_chain) == 4


def test_redirect_to_private_address_is_blocked_even_from_public_start() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    fetcher = make_fetcher(handler, allow_private=False)
    result = fetcher.fetch("https://93.184.215.14/")
    assert result.error_kind == "blocked"
    assert "169.254.169.254" in (result.error or "")


def test_response_size_limit_truncates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 10_000)

    result = make_fetcher(handler, max_bytes=1000).fetch("https://example.com/big")
    assert result.truncated
    assert len(result.body) == 1000


def test_head_without_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        return httpx.Response(200)

    result = make_fetcher(handler).fetch("https://example.com/", method="HEAD", read_body=False)
    assert result.status_code == 200 and result.body == b""


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx.ConnectTimeout("timed out"), "timeout"),
        (httpx.ConnectError("connection refused"), "connection"),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), "ssl"),
        (httpx.RemoteProtocolError("bad response"), "http"),
    ],
)
def test_network_errors_are_returned_not_raised(exc: Exception, kind: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    result = make_fetcher(handler).fetch("https://example.com/")
    assert result.error_kind == kind
    assert not result.ok


def test_multiple_x_robots_tag_headers_are_newline_joined() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers=[("X-Robots-Tag", "googlebot: noindex"), ("X-Robots-Tag", "noarchive")]
        )

    result = make_fetcher(handler).fetch("https://example.com/")
    assert result.headers["x-robots-tag"] == "googlebot: noindex\nnoarchive"


def test_invalid_config_is_rejected() -> None:
    with pytest.raises(ValueError):
        FetchConfig(timeout=0)
    with pytest.raises(ValueError):
        FetchConfig(max_bytes=0)


def test_redirect_to_non_http_scheme_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "file:///etc/passwd"})

    result = make_fetcher(handler).fetch("https://example.com/")
    assert result.error_kind == "blocked"


def test_slow_drip_body_hits_total_deadline() -> None:
    ticks = iter(float(n) for n in range(1000))  # every clock read advances one second

    class Drip(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            for _ in range(100):
                yield b"x"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Drip())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(FetchConfig(delay=0, allow_private=True, timeout=5), client=client,
                      clock=lambda: next(ticks))  # fmt: skip
    result = fetcher.fetch("https://example.com/")
    assert result.error_kind == "timeout"
    assert "not received within 15s" in (result.error or "")
