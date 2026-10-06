import ipaddress
import socket

import pytest

from web_visibility import safety
from web_visibility.safety import (
    HostResolutionError,
    UnsafeURLError,
    check_url_safety,
    is_local_host,
    non_public_reason,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.8.9.1:8080/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.1/admin",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fd00:ec2::254]/",  # AWS IPv6 metadata (unique local)
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "http://100.64.0.1/",  # carrier-grade NAT
        "http://224.0.0.1/",  # multicast
    ],
)
def test_private_ip_literals_are_blocked(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        check_url_safety(url)


def test_public_ip_literal_is_allowed() -> None:
    check_url_safety("https://93.184.215.14/")


def test_hostname_resolving_to_private_address_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError, match="loopback"):
        check_url_safety("http://innocent-looking.example/")


def test_any_private_address_in_the_answer_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError):
        check_url_safety("http://mixed.example/")


def test_dns_failure_raises_resolution_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(*_: object, **__: object) -> None:
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(HostResolutionError):
        check_url_safety("https://does-not-exist.invalid/")


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/", "https://user:pw@example.com/"]
)
def test_disallowed_schemes_and_credentials(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        check_url_safety(url, allow_private=True)


def test_allow_private_opt_in_skips_address_checks() -> None:
    check_url_safety("http://127.0.0.1:8000/", allow_private=True)


def test_non_public_reason() -> None:
    assert non_public_reason(ipaddress.ip_address("8.8.8.8")) is None
    assert non_public_reason(ipaddress.ip_address("127.0.0.1")) == "loopback"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("app.localhost", True),
        ("127.0.0.1", True),
        ("192.168.0.10", True),
        ("example.com", False),
        ("8.8.8.8", False),
    ],
)
def test_is_local_host(host: str, expected: bool) -> None:
    assert is_local_host(host) is expected


# --- M5 hardening: ranges, pinning, rebinding, DNS timeout ------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "127.255.255.254", "10.0.0.1", "10.255.255.255", "172.16.0.1",
        "172.31.255.255", "192.168.0.1", "169.254.169.254", "0.0.0.0", "100.64.0.1",
        "::1", "fc00::1", "fd12:3456::1", "fe80::1", "::ffff:10.0.0.1",
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
        "2002:7f00:1::1",  # 6to4 wrapping 127.0.0.1
        "2002:a00:1::1",  # 6to4 wrapping 10.0.0.1
    ],
)  # fmt: skip
def test_special_use_addresses_are_blocked(address: str) -> None:
    assert non_public_reason(ipaddress.ip_address(address)) is not None


@pytest.mark.parametrize(
    "address",
    ["8.8.8.8", "1.1.1.1", "93.184.215.14", "172.32.0.1", "100.128.0.1", "192.169.0.1",
     "2606:4700:4700::1111", "2001:4860:4860::8888", "64:ff9b::808:808"],
)  # fmt: skip
def test_public_addresses_are_not_blocked(address: str) -> None:
    assert non_public_reason(ipaddress.ip_address(address)) is None


def _public_dns(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    """Each lookup pops the next answer, so a second lookup can 'rebind'."""
    calls: list[str] = []

    def fake_getaddrinfo(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        calls.append(host)
        address = answers.pop(0) if len(answers) > 1 else answers[0]
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return calls


def test_fetcher_connects_to_the_validated_address(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from web_visibility.fetch import FetchConfig, Fetcher

    calls = _public_dns(monkeypatch, ["93.184.215.14"])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(
        FetchConfig(delay=0), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = fetcher.fetch("https://example.com/page?q=1")
    assert result.status_code == 200 and result.final_url == "https://example.com/page?q=1"
    request = seen[0]
    assert request.url.host == "93.184.215.14"  # no second DNS lookup by the HTTP client
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"  # TLS still verifies the name
    assert calls == ["example.com"]


def test_rebinding_on_a_redirect_hop_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from web_visibility.fetch import FetchConfig, Fetcher

    _public_dns(monkeypatch, ["93.184.215.14", "127.0.0.1"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://rebind.example/admin"})

    fetcher = Fetcher(
        FetchConfig(delay=0), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = fetcher.fetch("https://example.com/")
    assert result.error_kind == "blocked"
    assert "127.0.0.1" in (result.error or "")


def test_dns_resolution_obeys_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    release = threading.Event()

    def slow_getaddrinfo(*_: object, **__: object) -> None:
        release.wait(5)

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
    try:
        with pytest.raises(HostResolutionError, match="timed out"):
            safety.resolve_host("slow.example", 443, timeout=0.2)
    finally:
        release.set()


def test_ipv6_literal_pinning_uses_brackets() -> None:
    from web_visibility.fetch import _pinned_request

    url, headers, extensions = _pinned_request("https://example.com:8443/x", "2606:4700::1")
    assert url == "https://[2606:4700::1]:8443/x"
    assert headers == {"Host": "example.com:8443"}
    assert extensions == {"sni_hostname": "example.com"}
