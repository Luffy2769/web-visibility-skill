"""URL normalization and classification.

Normalization exists to stop the crawler from treating trivially different
spellings of one URL as different pages. It is deliberately conservative: it
only applies transformations that do not change what a server receives in a
meaningful way (RFC 3986 section 6.2.2 plus fragment and tracking-parameter
removal). In particular it does NOT:

* add or remove trailing slashes (``/about`` and ``/about/`` can differ),
* reorder query parameters,
* upgrade ``http`` to ``https``,
* strip ``www.``.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}

# Parameters that only carry campaign/click attribution. Removing them never
# changes the resource, and keeping them makes one page look like many.
TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "fbclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
    }
)
TRACKING_PREFIXES = ("utm_",)

# Targets with these extensions are link-checked but never parsed as pages.
NON_HTML_EXTENSIONS = frozenset(
    {
        ".7z",
        ".avi",
        ".avif",
        ".bmp",
        ".css",
        ".csv",
        ".doc",
        ".docx",
        ".dmg",
        ".eot",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".m4a",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".ogg",
        ".otf",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".rar",
        ".rss",
        ".svg",
        ".tar",
        ".tif",
        ".tiff",
        ".ttf",
        ".txt",
        ".wav",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
        ".xls",
        ".xlsx",
        ".xml",
        ".zip",
    }
)

_SKIPPED_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "sms:", "ftp:", "file:", "blob:")
_PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
# Characters allowed to stay literal when re-quoting a path or query.
_PATH_SAFE = "/:@!$&'()*+,;=-._~%"
_QUERY_SAFE = _PATH_SAFE + "?"


def is_skipped_scheme(href: str) -> bool:
    """True for hrefs that are never HTTP resources (mailto:, tel:, javascript: ...)."""
    return href.strip().lower().startswith(_SKIPPED_SCHEMES)


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Return an absolute, normalized HTTP(S) URL, or ``None`` if ``url`` is not one.

    ``base`` is used to resolve relative references.
    """
    url = url.strip()
    if not url and base is None:
        return None
    if is_skipped_scheme(url):
        return None
    if base is not None:
        url = urljoin(base, url)

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        return None
    if parts.username is not None or parts.password is not None:
        return None  # credentials in URLs are refused outright

    host = parts.hostname
    if not host:
        return None
    try:
        host = host.encode("idna").decode("ascii") if not _is_ip_literal(host) else host
    except UnicodeError:
        return None

    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"

    path = normalize_percent(quote(_remove_dot_segments(parts.path), safe=_PATH_SAFE)) or "/"
    query = _strip_tracking(normalize_percent(quote(parts.query, safe=_QUERY_SAFE)))
    return urlunsplit((scheme, netloc, path, query, ""))


def origin(url: str) -> str:
    """``scheme://host[:port]`` of a normalized URL."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def same_origin(a: str, b: str) -> bool:
    return origin(a) == origin(b)


def same_host(a: str, b: str) -> bool:
    return host_of(a) == host_of(b)


def same_site(a: str, b: str) -> bool:
    """True when two URLs are on the same host, ignoring one leading ``www.``.

    Used only to decide whether a redirected start URL still belongs to the site
    the user asked about (``http://example.com`` -> ``https://www.example.com``).
    Deliberately stricter than "same registrable domain": without the Public
    Suffix List, ``user.github.io`` and ``github.io`` cannot be told apart from
    ``shop.example.com`` and ``example.com``, so subdomain moves are not adopted.
    """
    host_a, host_b = host_of(a), host_of(b)
    if not host_a or not host_b:
        return False
    return host_a.removeprefix("www.") == host_b.removeprefix("www.")


def has_non_html_extension(url: str) -> bool:
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    return dot > path.rfind("/") and path[dot:] in NON_HTML_EXTENSIONS


def path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _is_ip_literal(host: str) -> bool:
    return ":" in host or host.replace(".", "").isdigit()


def normalize_percent(value: str) -> str:
    """Upper-case percent escapes and decode escaped unreserved characters."""

    def fix(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"

    return _PERCENT_ESCAPE.sub(fix, value)


def _strip_tracking(query: str) -> str:
    if not query:
        return ""
    kept = []
    for pair in query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0].lower()
        if key in TRACKING_PARAMS or key.startswith(TRACKING_PREFIXES):
            continue
        kept.append(pair)
    return "&".join(kept)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4."""
    if "." not in path:
        return path
    output: list[str] = []
    segments = path.split("/")
    for index, segment in enumerate(segments):
        is_last = index == len(segments) - 1
        if segment == ".":
            if is_last:
                output.append("")
            continue
        if segment == "..":
            if len(output) > 1:
                output.pop()
            if is_last:
                output.append("")
            continue
        output.append(segment)
    result = "/".join(output)
    if path.startswith("/") and not result.startswith("/"):
        result = "/" + result
    return result
