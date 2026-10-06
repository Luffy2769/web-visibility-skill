"""Server-side request forgery (SSRF) guard.

The auditor fetches attacker-influenced URLs: the user supplies the start URL,
but the *website* supplies every link, redirect and sitemap entry. A hostile
page could point the crawler at ``http://169.254.169.254/`` (cloud metadata),
``http://localhost:6379/`` or a router admin page.

:func:`resolve_target` resolves a host **once** (with a timeout), requires every
returned address to be publicly routable, and returns the validated address.
The fetcher then connects to that exact address (sending the original ``Host``
header and TLS server name), so a DNS answer cannot change between the check and
the connection (DNS rebinding). The check runs for every redirect hop.

Auditing a local development server requires the explicit ``allow_private``
opt-in (``--allow-private-network``); in that mode no address checks or pinning
are performed.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from web_visibility.urls import DEFAULT_PORTS

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public network location."""


class HostResolutionError(OSError):
    """Raised when a hostname cannot be resolved (or resolution timed out)."""


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    host: str
    port: int
    address: str | None
    """Validated IP to connect to; ``None`` when private targets are allowed."""


def resolve_target(
    url: str, *, allow_private: bool = False, timeout: float = 10.0
) -> ResolvedTarget:
    """Validate ``url`` and return the address it may be fetched from.

    Raises :class:`UnsafeURLError` for disallowed URLs and
    :class:`HostResolutionError` if the host does not resolve in time.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError(f"malformed URL: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise UnsafeURLError(f"scheme {scheme!r} is not allowed (only http and https)")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("URLs containing credentials are not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    port = port or DEFAULT_PORTS[scheme]
    if allow_private:
        return ResolvedTarget(host, port, None)

    addresses = resolve_host(host, port, timeout=timeout)
    for address in addresses:
        reason = non_public_reason(address)
        if reason:
            raise UnsafeURLError(
                f"{host} resolves to {address} ({reason}); refusing to fetch. "
                "Use --allow-private-network to audit local or internal sites."
            )
    return ResolvedTarget(host, port, str(addresses[0]))


def check_url_safety(url: str, *, allow_private: bool = False, timeout: float = 10.0) -> None:
    """Raise unless ``url`` may be fetched (see :func:`resolve_target`)."""
    resolve_target(url, allow_private=allow_private, timeout=timeout)


def resolve_host(host: str, port: int, *, timeout: float = 10.0) -> list[IPAddress]:
    """Every address ``host`` resolves to, in resolver order (IP literals as-is)."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    infos = _getaddrinfo_with_timeout(host, port, timeout)
    addresses: list[IPAddress] = []
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])  # drop IPv6 zone id
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise HostResolutionError(f"host {host!r} resolved to no addresses")
    return addresses


def non_public_reason(address: IPAddress) -> str | None:
    """Why ``address`` is not publicly routable, or ``None`` if it is.

    IPv6 forms that embed an IPv4 address (IPv4-mapped, NAT64, 6to4, Teredo) are
    judged by the embedded address as well.
    """
    for embedded in _embedded_ipv4(address):
        reason = non_public_reason(embedded)
        if reason:
            return f"{reason} (embedded in {address})"
    if isinstance(address, ipaddress.IPv6Address) and address in _NAT64:
        # Well-known NAT64 prefix (DNS64 networks map every IPv4 site into it):
        # the embedded IPv4 address, checked above, is what will actually be reached.
        return None
    checks = (
        ("loopback", address.is_loopback),
        ("private network", address.is_private),
        ("link-local / cloud metadata range", address.is_link_local),
        ("multicast", address.is_multicast),
        ("reserved", address.is_reserved),
        ("unspecified", address.is_unspecified),
        ("site-local", isinstance(address, ipaddress.IPv6Address) and address.is_site_local),
        ("not globally routable", not address.is_global),
    )
    for reason, matched in checks:
        if matched:
            return reason
    return None


def is_local_host(host: str) -> bool:
    """True for ``localhost`` names and non-public IP literals (no DNS lookup)."""
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test")):
        return True
    try:
        return non_public_reason(ipaddress.ip_address(host)) is not None
    except ValueError:
        return False


def _embedded_ipv4(address: IPAddress) -> list[ipaddress.IPv4Address]:
    if not isinstance(address, ipaddress.IPv6Address):
        return []
    found = []
    if address.ipv4_mapped is not None:
        found.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address.teredo is not None:
        found.extend(address.teredo)
    if address in _NAT64 or address in _NAT64_LOCAL:
        found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return found


def _getaddrinfo_with_timeout(host: str, port: int, timeout: float) -> list[Any]:
    """``socket.getaddrinfo`` has no timeout; run it in a worker thread with one."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["infos"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, name="dns-resolve", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise HostResolutionError(f"resolving {host!r} timed out after {timeout:g}s")
    if "error" in outcome:
        raise HostResolutionError(f"could not resolve host {host!r}: {outcome['error']}")
    return list(outcome["infos"])
