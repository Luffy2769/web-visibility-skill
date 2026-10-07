"""robots.txt parsing and per-crawler evaluation (RFC 9309).

Implemented:

* groups of one or more ``User-agent`` lines followed by ``Allow``/``Disallow``,
* group selection by **exact, case-insensitive product-token match**
  (``Googlebot/2.1`` in a file matches the ``googlebot`` token); groups naming
  the same agent are merged; the ``*`` group applies only when no group names
  the crawler,
* ``*`` wildcards and the ``$`` end anchor,
* longest-match precedence, with ``Allow`` winning ties; an empty ``Disallow``
  allows everything; ``/robots.txt`` is always allowed,
* percent-encoding normalization of rule paths and URLs (``/%7Ejoe`` matches
  ``/~joe``),
* the global ``Sitemap`` directive and the non-standard ``Crawl-delay``.

Intentionally not implemented: crawler-specific fallbacks that some engines use
(e.g. ``googlebot-image`` falling back to the ``googlebot`` group), ``Host`` and
``Clean-param``. Findings therefore say what the file *appears* to permit for a
named token; they never predict what a search engine will index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from web_visibility.urls import normalize_percent, path_and_query

MAX_ROBOTS_BYTES = 500_000  # RFC 9309: parse at least the first 500 KiB

CrawlerPurpose = Literal["search", "ai", "auditor"]


@dataclass(frozen=True, slots=True)
class CrawlerProfile:
    token: str
    """Product token as written in robots.txt."""
    purpose: CrawlerPurpose
    operator: str


#: Crawler identities evaluated in every audit. ``Google-Extended`` is a robots.txt
#: control token (Google AI use), not a separate crawler, but is evaluated the same way.
EVALUATED_CRAWLERS: tuple[CrawlerProfile, ...] = (
    CrawlerProfile("Googlebot", "search", "Google"),
    CrawlerProfile("Bingbot", "search", "Microsoft"),
    CrawlerProfile("GPTBot", "ai", "OpenAI"),
    CrawlerProfile("OAI-SearchBot", "ai", "OpenAI"),
    CrawlerProfile("ClaudeBot", "ai", "Anthropic"),
    CrawlerProfile("PerplexityBot", "ai", "Perplexity"),
    CrawlerProfile("Google-Extended", "ai", "Google"),
    CrawlerProfile("CCBot", "ai", "Common Crawl"),
)
SEARCH_CRAWLERS = tuple(c for c in EVALUATED_CRAWLERS if c.purpose == "search")
AI_CRAWLERS = tuple(c for c in EVALUATED_CRAWLERS if c.purpose == "ai")


@dataclass(frozen=True, slots=True)
class RobotsRule:
    allow: bool
    path: str
    """Normalized path pattern used for matching."""
    raw: str = ""
    """The path as written in the file (for evidence)."""

    def matches(self, target: str) -> bool:
        return wildcard_match(self.path, target)

    def __str__(self) -> str:
        return f"{'Allow' if self.allow else 'Disallow'}: {self.raw or self.path}"


@dataclass(frozen=True, slots=True)
class RobotsGroup:
    user_agents: tuple[str, ...]
    """Lower-cased product tokens (``*`` for the wildcard group)."""
    rules: tuple[RobotsRule, ...]
    crawl_delay: float | None = None


@dataclass(frozen=True, slots=True)
class RobotsVerdict:
    """Whether ``agent`` may fetch a path, and the evidence for that answer."""

    agent: str
    allowed: bool
    group: str | None
    """``"*"``, the agent's own token, or ``None`` when no group applies."""
    rule: RobotsRule | None
    """The deciding rule; ``None`` means no rule matched (allowed by default)."""

    @property
    def explanation(self) -> str:
        if self.group is None:
            return "no group applies (allowed)"
        group = "User-agent: *" if self.group == "*" else f"User-agent: {self.group}"
        if self.rule is None:
            return f"{group} has no matching rule (allowed)"
        return f"{group} -> {self.rule}"


@dataclass(frozen=True)
class RobotsTxt:
    groups: tuple[RobotsGroup, ...] = ()
    sitemaps: tuple[str, ...] = ()
    ignored_lines: int = 0
    """Non-empty lines that were not a recognised ``key: value`` directive."""

    @classmethod
    def parse(cls, text: str) -> RobotsTxt:
        groups: list[RobotsGroup] = []
        sitemaps: list[str] = []
        agents: list[str] = []
        rules: list[RobotsRule] = []
        delay: float | None = None
        ignored = 0
        in_rules = False

        def flush() -> None:
            if agents:
                groups.append(RobotsGroup(tuple(agents), tuple(rules), delay))

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            key, sep, value = line.partition(":")
            if not sep:
                ignored += 1
                continue
            key = key.strip().lower()
            value = value.strip()

            if key == "user-agent":
                if in_rules:  # a user-agent line after rules starts a new group
                    flush()
                    agents.clear()
                    rules.clear()
                    delay = None
                    in_rules = False
                agents.append(_product_token(value))
            elif key in ("allow", "disallow"):
                in_rules = True
                if agents and value:  # an empty Disallow means "allow everything"
                    rules.append(
                        RobotsRule(
                            allow=key == "allow", path=normalize_robots_path(value), raw=value
                        )
                    )
            elif key == "crawl-delay":
                in_rules = True
                if agents:
                    delay = _parse_delay(value)
            elif key == "sitemap":
                if value:
                    sitemaps.append(value)
            elif key in ("host", "clean-param", "request-rate", "visit-time"):
                continue  # known non-standard extensions, not modelled
            else:
                ignored += 1
        flush()
        return cls(tuple(groups), tuple(dict.fromkeys(sitemaps)), ignored)

    # --- group selection --------------------------------------------------- #

    def groups_for(self, user_agent: str) -> tuple[tuple[RobotsGroup, ...], str | None]:
        """Groups that apply to ``user_agent`` and the label of the match."""
        token = _product_token(user_agent)
        specific = tuple(g for g in self.groups if token in g.user_agents)
        if specific:
            return specific, token
        wildcard = tuple(g for g in self.groups if "*" in g.user_agents)
        return wildcard, ("*" if wildcard else None)

    def rules_for(self, user_agent: str) -> tuple[RobotsRule, ...]:
        groups, _ = self.groups_for(user_agent)
        return tuple(rule for group in groups for rule in group.rules)

    def crawl_delay(self, user_agent: str) -> float | None:
        groups, _ = self.groups_for(user_agent)
        delays = [g.crawl_delay for g in groups if g.crawl_delay is not None]
        return max(delays) if delays else None

    def names_agent(self, user_agent: str) -> bool:
        """True when a group names this agent explicitly (not via ``*``)."""
        return self.groups_for(user_agent)[1] == _product_token(user_agent)

    # --- evaluation -------------------------------------------------------- #

    def verdict(self, url_or_path: str, user_agent: str = "*") -> RobotsVerdict:
        target = url_or_path if url_or_path.startswith("/") else path_and_query(url_or_path)
        target = normalize_robots_path(target)
        groups, label = self.groups_for(user_agent)
        if target == "/robots.txt":
            return RobotsVerdict(user_agent, True, label, None)
        best: RobotsRule | None = None
        for rule in (r for g in groups for r in g.rules):
            if not rule.matches(target):
                continue
            longer = best is None or len(rule.path) > len(best.path)
            tie_allow = best is not None and len(rule.path) == len(best.path) and rule.allow
            if longer or tie_allow:
                best = rule
        return RobotsVerdict(user_agent, best is None or best.allow, label, best)

    def is_allowed(self, url_or_path: str, user_agent: str = "*") -> bool:
        return self.verdict(url_or_path, user_agent).allowed

    def disallows_everything(self, user_agent: str = "*") -> bool:
        """True when a rule matching *every* path (``/`` or ``/*``) decides ``/`` and the
        applicable group has no ``Allow`` rules. ``Disallow: /$`` (homepage only) is not
        "everything"."""
        rules = self.rules_for(user_agent)
        verdict = self.verdict("/", user_agent)
        matches_all = verdict.rule is not None and verdict.rule.path.rstrip("*") == "/"
        return not verdict.allowed and matches_all and not any(r.allow for r in rules)

    @property
    def has_rules(self) -> bool:
        return any(group.rules for group in self.groups)


def normalize_robots_path(path: str) -> str:
    """Normalize percent-encoding so equivalent spellings compare equal.

    Escapes of unreserved characters are decoded (``%7E`` -> ``~``), other escapes
    are upper-cased, and characters that must be encoded are encoded. ``*`` and
    ``$`` keep their pattern meaning.
    """
    return normalize_percent(quote(path, safe="/:@!$&'()*+,;=-._~%?"))


def _product_token(value: str) -> str:
    """``"Googlebot/2.1 (+http://...)"`` -> ``"googlebot"``."""
    token = value.strip().split("/", 1)[0].split(" ", 1)[0].strip().lower()
    return token or "*"


def _parse_delay(value: str) -> float | None:
    try:
        delay = float(value)
    except ValueError:
        return None
    return delay if delay >= 0 else None


def wildcard_match(pattern: str, target: str) -> bool:
    """Does robots.txt ``pattern`` (``*`` wildcards, optional trailing ``$``) match
    ``target`` from its start?

    Linear time. ``pattern`` and ``target`` both come from the audited site, so a
    backtracking regex (``.*a.*a.*a...``) would let a hostile robots.txt plus one
    crafted link hang the audit. With only ``*`` and an end anchor, matching the
    ``*``-separated segments greedily at their leftmost position is exact.
    """
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    parts = body.split("*")
    if len(parts) == 1:
        return target == body if anchored else target.startswith(body)
    first, *middle, last = parts
    if not target.startswith(first):
        return False
    position, end = len(first), len(target)
    if anchored:
        end -= len(last)
        if end < position or not target.endswith(last):
            return False
    for segment in middle:
        if segment:
            found = target.find(segment, position, end)
            if found < 0:
                return False
            position = found + len(segment)
    return anchored or target.find(last, position) >= 0
