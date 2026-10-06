"""The robots.txt retrieval outcome."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from web_visibility.robots import RobotsTxt, RobotsVerdict

RobotsState = Literal[
    "parsed",  # 2xx: rules parsed
    "missing",  # 404/410 and other 4xx (except below): no restrictions
    "inaccessible",  # 401/403: no restrictions per RFC 9309, but worth reporting
    "rate-limited",  # 429: treated like a server error (do not crawl)
    "server-error",  # 5xx: RFC 9309 says assume complete disallow
    "unavailable",  # timeout / DNS / connection / TLS failure: assume complete disallow
    "blocked",  # refused by the SSRF guard; nothing was requested
]

#: States in which a polite crawler must assume it may fetch nothing.
RESTRICTIVE_STATES: frozenset[RobotsState] = frozenset(
    {"rate-limited", "server-error", "unavailable"}
)


def classify_robots_response(status: int | None, error_kind: str | None) -> RobotsState:
    if error_kind == "blocked":
        return "blocked"
    if status is None:
        return "unavailable"
    if 200 <= status < 300:
        return "parsed"
    if status == 429:
        return "rate-limited"
    if status >= 500:
        return "server-error"
    if status in (401, 403):
        return "inaccessible"
    return "missing"


@dataclass(frozen=True, slots=True)
class RobotsResult:
    url: str
    state: RobotsState
    status_code: int | None = None
    content_type: str | None = None
    error: str | None = None
    error_kind: str | None = None
    text: str | None = None
    rules: RobotsTxt | None = None
    looks_like_html: bool = False
    truncated: bool = False

    @property
    def exists(self) -> bool:
        return self.state == "parsed"

    @property
    def restricts_crawling(self) -> bool:
        """True when crawlers should assume a complete disallow (RFC 9309 sec. 2.3.1.4)."""
        return self.state in RESTRICTIVE_STATES

    def verdict(self, url: str, user_agent: str) -> RobotsVerdict:
        if self.rules is not None:
            return self.rules.verdict(url, user_agent)
        return RobotsVerdict(user_agent, not self.restricts_crawling, None, None)

    def allows(self, url: str, user_agent: str = "*") -> bool:
        return self.verdict(url, user_agent).allowed

    @property
    def skip_reason(self) -> str:
        """Why a URL this robots.txt does not allow was skipped."""
        return "robots-disallowed" if self.rules is not None else "robots-unavailable"
