"""Internal/external links, broken-link candidates, anchors and orphan signals.

Broken-link policy (a target is only "broken" when the evidence supports it):

* 404 / 410                 -> broken, high (internal), confidence 0.95
* other 4xx                 -> broken candidate, medium, confidence 0.8
* 5xx                       -> broken candidate, medium, confidence 0.6 (may be transient)
* redirect loop / too many  -> broken candidate, medium, confidence 0.85
* 401 / 403                 -> unverified: access restricted, not necessarily broken (info)
* 429                       -> unverified: rate limited (info)
* timeout / network error   -> unverified (info)
* 3xx ending in 2xx         -> working; internal links get a low "points to a redirect" note

External links are only checked with ``--check-external`` and are capped at low severity.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from web_visibility.analyzers.base import (
    AuditContext,
    TargetStatus,
    joined_list,
    plural,
    quoted_list,
    sample,
)
from web_visibility.models import Category, Issue, Link, Page, Rule, Severity
from web_visibility.urls import same_host

C = Category.LINKS

GENERIC_ANCHORS = frozenset(
    {
        "click here",
        "here",
        "read more",
        "more",
        "learn more",
        "this link",
        "link",
        "this",
        "go",
        "click",
        "details",
        "more info",
    }
)

BROKEN_INTERNAL = Rule(
    "broken-internal-link",
    C,
    Severity.HIGH,
    "Broken internal link",
    "Update or remove links to this URL, or restore/redirect the missing page.",
)
UNVERIFIED_LINK = Rule(
    "unverified-link",
    C,
    Severity.INFO,
    "Link target could not be verified",
    "Check the target manually; the response did not prove it broken or working.",
)
INTERNAL_REDIRECT = Rule(
    "internal-link-redirect",
    C,
    Severity.LOW,
    "Internal link points to a redirect",
    "Link directly to the final URL to avoid an extra request.",
)
INSECURE_INTERNAL = Rule(
    "insecure-internal-link",
    C,
    Severity.LOW,
    "Internal links use http:// on an HTTPS site",
    "Change internal links to https:// so visitors are not routed through an insecure hop.",
)
BROKEN_EXTERNAL = Rule(
    "broken-external-link",
    C,
    Severity.LOW,
    "Broken external link",
    "Update the link to a working source or remove it.",
)
EMPTY_ANCHOR = Rule(
    "empty-anchor-text",
    C,
    Severity.LOW,
    "Links without an accessible name",
    "Give each link visible text, an aria-label, or (for image links) descriptive alt text.",
)
GENERIC_ANCHOR = Rule(
    "generic-anchor-text",
    C,
    Severity.INFO,
    "Generic link text",
    'Prefer anchor text that describes the destination over phrases like "click here".',
)
POTENTIAL_ORPHAN = Rule(
    "potential-orphan-page",
    C,
    Severity.LOW,
    "Potential orphan page",
    "If this page matters, link to it from relevant pages so users and crawlers can reach it.",
)
NO_OUTLINKS = Rule(
    "no-internal-outlinks",
    C,
    Severity.LOW,
    "Page has no internal links",
    "Add navigation or contextual links so the page is not a dead end.",
)
INTERNAL_NOFOLLOW = Rule(
    "internal-nofollow",
    C,
    Severity.INFO,
    "Internal links marked nofollow",
    "Nofollow on internal links is rarely needed; confirm it is intentional.",
)
UNCHECKED_LINKS = Rule(
    "links-not-checked",
    C,
    Severity.INFO,
    "Some link targets were not checked",
    "Raise --max-link-checks (or enable --check-external) for a complete link check.",
)

RULES = (
    BROKEN_INTERNAL,
    UNVERIFIED_LINK,
    INTERNAL_REDIRECT,
    INSECURE_INTERNAL,
    BROKEN_EXTERNAL,
    EMPTY_ANCHOR,
    GENERIC_ANCHOR,
    POTENTIAL_ORPHAN,
    NO_OUTLINKS,
    INTERNAL_NOFOLLOW,
    UNCHECKED_LINKS,
)


@dataclass(frozen=True, slots=True)
class LinkVerdict:
    broken: bool
    severity: Severity
    confidence: float
    reason: str


def classify_target(status: TargetStatus) -> LinkVerdict | None:
    """Interpret a response. ``None`` means the target works (2xx after redirects)."""
    if status.error is not None:
        if status.error_kind in ("redirect-loop", "too-many-redirects"):
            return LinkVerdict(True, Severity.MEDIUM, 0.85, status.error)
        reason = {
            "blocked": "not fetched: the target resolves to a non-public address",
            "timeout": "request timed out",
        }.get(status.error_kind or "", status.error)
        return LinkVerdict(False, Severity.INFO, 0.5, reason)

    code = status.status_code
    if code is None or code < 400:
        return None
    if code in (404, 410):
        return LinkVerdict(True, Severity.HIGH, 0.95, f"HTTP {code}")
    if code in (401, 403):
        return LinkVerdict(
            False, Severity.INFO, 0.5, f"HTTP {code}: access restricted, not necessarily broken"
        )
    if code == 429:
        return LinkVerdict(False, Severity.INFO, 0.5, "HTTP 429: rate limited during the audit")
    if code >= 500:
        return LinkVerdict(
            True,
            Severity.MEDIUM,
            0.6,
            f"HTTP {code}: server error at audit time (may be transient)",
        )
    return LinkVerdict(True, Severity.MEDIUM, 0.8, f"HTTP {code}")


def analyze(ctx: AuditContext) -> list[Issue]:
    issues: list[Issue] = []
    issues += _target_issues(ctx)
    for page in ctx.pages:
        issues += _page_issues(ctx, page)
    issues += _orphans(ctx)
    if ctx.crawl.unchecked_links:
        unchecked = ctx.crawl.unchecked_links
        issues.append(
            UNCHECKED_LINKS.issue(
                description=f"{plural(len(unchecked), 'link target')} exceeded the link-check "
                "budget.",
                evidence=f"Not checked: {joined_list(unchecked)}",
                confidence=1.0,
                details={"count": len(unchecked), "urls": list(unchecked)},
            )
        )
    return issues


# --------------------------------------------------------------------------- #


def _target_issues(ctx: AuditContext) -> list[Issue]:
    """One issue per problematic link target, listing every page that links to it."""
    sources: defaultdict[str, dict[str, None]] = defaultdict(dict)
    anchors: defaultdict[str, dict[str, None]] = defaultdict(dict)
    for page in ctx.pages:
        for link in page.links:
            sources[link.url][page.final_url] = None
            if link.text:
                anchors[link.url][link.text] = None

    coverage = ctx.link_coverage()
    issues: list[Issue] = []
    for target, linked_from in sources.items():
        status = ctx.target_status(target)
        if status is None:
            continue
        internal = same_host(target, ctx.site_url)
        # Each target is one of N checked targets: 19 broken of 20 checked costs 19/20.
        prevalence = 1 / max(
            1, coverage.internal_checked if internal else coverage.external_checked
        )
        from_urls = tuple(linked_from)
        details = {
            "target": target,
            "status": status.status_code,
            "linked_from": list(from_urls),
            "anchor_texts": sample(anchors[target], 10),
        }
        verdict = classify_target(status)
        where = f"linked from {plural(len(from_urls), 'page')}: {joined_list(from_urls)}"

        if verdict is None:
            if internal and status.redirected:
                issues.append(
                    INTERNAL_REDIRECT.issue(
                        description=f"Links point to {target}, which redirects.",
                        evidence=f"{target} redirects to {status.final_url}; {where}.",
                        confidence=0.9,
                        affected_urls=from_urls,
                        details={**details, "final_url": status.final_url},
                        prevalence=prevalence,
                    )
                )
            continue
        if not verdict.broken:
            issues.append(
                UNVERIFIED_LINK.issue(
                    description=f"{target} could not be verified ({verdict.reason}).",
                    evidence=f"{target}: {verdict.reason}; {where}.",
                    confidence=verdict.confidence,
                    affected_urls=from_urls,
                    details={**details, "reason": verdict.reason},
                )
            )
            continue
        rule = BROKEN_INTERNAL if internal else BROKEN_EXTERNAL
        severity = verdict.severity if internal else Severity.LOW
        issues.append(
            rule.issue(
                description=f"Link target {target} appears broken ({verdict.reason}).",
                evidence=f"{target} returned {verdict.reason}; {where}.",
                confidence=verdict.confidence,
                affected_urls=from_urls,
                severity=severity,
                details={**details, "reason": verdict.reason},
                prevalence=prevalence,
            )
        )
    return issues


def _page_issues(ctx: AuditContext, page: Page) -> list[Issue]:
    url = (page.final_url,)
    issues: list[Issue] = []
    internal = [link for link in page.internal_links if link.url != page.final_url]

    if not internal:
        issues.append(
            NO_OUTLINKS.issue(
                description="The page links to no other page on the site.",
                evidence=f"0 internal links to other pages ({len(page.links)} links in total).",
                confidence=0.9,
                affected_urls=url,
            )
        )

    if ctx.site_url.startswith("https://"):
        insecure = [link.url for link in page.internal_links if link.url.startswith("http://")]
        if insecure:
            issues.append(
                INSECURE_INTERNAL.issue(
                    description=f"{plural(len(insecure), 'internal link')} use http://.",
                    evidence=f"http:// links: {joined_list(insecure)}",
                    confidence=0.95,
                    affected_urls=url,
                    details={"links": sample(insecure, 20)},
                )
            )

    empty = [link for link in page.links if not link.text]
    if empty:
        issues.append(
            EMPTY_ANCHOR.issue(
                description=f"{plural(len(empty), 'link')} have no text, aria-label, title or "
                "image alt.",
                evidence=f"Links without an accessible name: {joined_list(_hrefs(empty))}",
                confidence=0.85,
                affected_urls=url,
                details={"links": sample(_hrefs(empty), 20)},
            )
        )

    generic = [link for link in page.links if link.text.casefold() in GENERIC_ANCHORS]
    if generic:
        issues.append(
            GENERIC_ANCHOR.issue(
                description=f"{plural(len(generic), 'link')} use generic anchor text.",
                evidence=f"Anchor texts: {quoted_list(link.text for link in generic)}",
                confidence=0.9,
                affected_urls=url,
            )
        )

    nofollow = [link.url for link in page.internal_links if link.nofollow]
    if nofollow:
        issues.append(
            INTERNAL_NOFOLLOW.issue(
                description=f'{plural(len(nofollow), "internal link")} carry rel="nofollow".',
                evidence=f"nofollow internal links: {joined_list(nofollow)}",
                confidence=0.95,
                affected_urls=url,
            )
        )
    return issues


def _orphans(ctx: AuditContext) -> list[Issue]:
    issues = []
    confidence = 0.5 if ctx.partial else 0.7
    caveat = (
        " The crawl reached its page limit, so pages that link here may not have been visited."
        if ctx.partial
        else ""
    )
    for page in ctx.pages:
        if page.discovered_via != "sitemap" or ctx.inbound_sources(page):
            continue
        issues.append(
            POTENTIAL_ORPHAN.issue(
                description="Potential orphan page based on the current crawl: it is listed in the "
                "sitemap but no crawled page links to it." + caveat,
                evidence=f"Discovered via sitemap; 0 inbound links among {ctx.page_count} "
                "crawled pages.",
                confidence=confidence,
                affected_urls=(page.final_url,),
                details={"pages_crawled": ctx.page_count, "crawl_limit_reached": ctx.partial},
            )
        )
    return issues


def _hrefs(links: list[Link]) -> list[str]:
    return [link.raw_href or link.url for link in links]
