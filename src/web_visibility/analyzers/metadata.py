"""Titles, meta descriptions and canonical URLs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from web_visibility.analyzers.base import AuditContext, joined_list, plural, quoted_list
from web_visibility.models import Category, Issue, Page, Rule, Severity
from web_visibility.urls import host_of, normalize_url

C = Category.METADATA

# Length thresholds are heuristics, not rules: search engines truncate titles
# by pixel width, and no length is "required". Findings say "may".
SHORT_TITLE_CHARS = 10
LONG_TITLE_CHARS = 70

MISSING_TITLE = Rule(
    "missing-title",
    C,
    Severity.HIGH,
    "Missing page title",
    "Add a unique, descriptive <title> element to the document <head>.",
)
EMPTY_TITLE = Rule(
    "empty-title",
    C,
    Severity.HIGH,
    "Empty page title",
    "Give the <title> element descriptive text that summarizes the page.",
)
MULTIPLE_TITLES = Rule(
    "multiple-titles",
    C,
    Severity.LOW,
    "Multiple <title> elements",
    "Keep a single <title> element; consumers may use any of them.",
)
SHORT_TITLE = Rule(
    "short-title",
    C,
    Severity.LOW,
    "Very short page title",
    "Consider a more descriptive title that states what the page is about.",
)
LONG_TITLE = Rule(
    "long-title",
    C,
    Severity.LOW,
    "Long page title",
    "Consider front-loading the most important words; long titles may be truncated in results.",
)
DUPLICATE_TITLE = Rule(
    "duplicate-title",
    C,
    Severity.MEDIUM,
    "Duplicate page titles",
    "Give each indexable page a title that distinguishes it from the others.",
)
MISSING_DESCRIPTION = Rule(
    "missing-meta-description",
    C,
    Severity.LOW,
    "Missing meta description",
    "Add a meta description summarizing the page; it may be used as the result snippet.",
)
EMPTY_DESCRIPTION = Rule(
    "empty-meta-description",
    C,
    Severity.LOW,
    "Empty meta description",
    "Fill in the meta description content or remove the empty tag.",
)
MULTIPLE_DESCRIPTIONS = Rule(
    "multiple-meta-descriptions",
    C,
    Severity.LOW,
    "Multiple meta descriptions",
    'Keep a single <meta name="description"> element.',
)
DUPLICATE_DESCRIPTION = Rule(
    "duplicate-meta-description",
    C,
    Severity.LOW,
    "Duplicate meta descriptions",
    "Write a description specific to each page, or omit it rather than reuse one.",
)
MISSING_CANONICAL = Rule(
    "missing-canonical",
    C,
    Severity.LOW,
    "Missing canonical URL",
    "If this page is intended to be indexed, declare its preferred URL with "
    '<link rel="canonical">.',
)
MULTIPLE_CANONICALS = Rule(
    "multiple-canonicals",
    C,
    Severity.MEDIUM,
    "Multiple canonical declarations",
    "Declare exactly one canonical URL per page; conflicting declarations may all be ignored.",
)
MALFORMED_CANONICAL = Rule(
    "malformed-canonical",
    C,
    Severity.MEDIUM,
    "Malformed canonical URL",
    "Point rel=canonical at an absolute http(s) URL.",
)
CANONICAL_TARGET_ERROR = Rule(
    "canonical-target-error",
    C,
    Severity.MEDIUM,
    "Canonical URL returns an error",
    "Point the canonical at a live (2xx) URL, normally this page or its preferred equivalent.",
)
CANONICAL_TARGET_REDIRECT = Rule(
    "canonical-target-redirects",
    C,
    Severity.LOW,
    "Canonical URL redirects",
    "Point the canonical directly at the final destination URL.",
)
CANONICAL_CROSS_HOST = Rule(
    "canonical-cross-host",
    C,
    Severity.INFO,
    "Canonical points to another host",
    "Confirm the cross-host canonical is intentional (e.g. syndicated content).",
)
CANONICALIZED = Rule(
    "canonicalized-to-other-url",
    C,
    Severity.INFO,
    "Page declares a different canonical URL",
    "No action needed if intentional; this page asks to be consolidated into another URL.",
)
CANONICAL_NOINDEX = Rule(
    "canonical-noindex-conflict",
    C,
    Severity.LOW,
    "noindex combined with a canonical to another URL",
    "Use either noindex or a canonical to another URL, not both; they send mixed signals.",
)

CANONICAL_MISPLACED = Rule(
    "canonical-outside-head",
    C,
    Severity.MEDIUM,
    "Canonical link outside <head>",
    'Move <link rel="canonical"> into <head>, before any body content. Canonicals in the '
    "body are generally not honoured.",
)
CANONICAL_HEADER_CONFLICT = Rule(
    "canonical-header-conflict",
    C,
    Severity.MEDIUM,
    "HTML and HTTP header canonicals disagree",
    "Declare the same canonical URL in both places, or use only one of them.",
)

RULES = (
    MISSING_TITLE,
    EMPTY_TITLE,
    MULTIPLE_TITLES,
    SHORT_TITLE,
    LONG_TITLE,
    DUPLICATE_TITLE,
    MISSING_DESCRIPTION,
    EMPTY_DESCRIPTION,
    MULTIPLE_DESCRIPTIONS,
    DUPLICATE_DESCRIPTION,
    MISSING_CANONICAL,
    MULTIPLE_CANONICALS,
    MALFORMED_CANONICAL,
    CANONICAL_TARGET_ERROR,
    CANONICAL_TARGET_REDIRECT,
    CANONICAL_CROSS_HOST,
    CANONICALIZED,
    CANONICAL_NOINDEX,
    CANONICAL_MISPLACED,
    CANONICAL_HEADER_CONFLICT,
)


def issues_for_misplaced_canonicals(page: Page) -> list[Issue]:
    assert page.content is not None
    body = [c for c in page.content.canonical_links if c.source == "body"]
    late = [c for c in page.content.canonical_links if c.source == "head-implicitly-closed"]
    issues = []
    if body:
        issues.append(CANONICAL_MISPLACED.issue(
            description="A canonical link appears in <body>, where it is generally not honoured.",
            evidence=f"rel=canonical in <body>: {quoted_list(str(c.href) for c in body)}.",
            confidence=0.9, affected_urls=(page.final_url,),
            details={"placement": "body", "canonicals": [c.href for c in body]},
        ))  # fmt: skip
    if late:
        anomalies = ", ".join(page.content.head_anomalies) or "body content"
        issues.append(CANONICAL_MISPLACED.issue(
            description="A canonical link is written in <head> but after an element that makes "
                        "HTML parsers end the head early, so browsers and search engines may "
                        "treat it as body content. Parser behaviour here varies.",
            evidence=f"<head> contains {anomalies} before rel=canonical "
                     f"{quoted_list(str(c.href) for c in late)}.",
            confidence=0.5, affected_urls=(page.final_url,),
            details={"placement": "head-implicitly-closed", "head_anomalies":
                     list(page.content.head_anomalies)},
        ))  # fmt: skip
    return issues


def analyze(ctx: AuditContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        issues += _title_issues(page)
        issues += _description_issues(page)
        issues += _canonical_issues(ctx, page)
    issues += _duplicates(ctx, DUPLICATE_TITLE, "title", lambda p: p.title)
    issues += _duplicates(
        ctx, DUPLICATE_DESCRIPTION, "meta description", lambda p: p.meta_description
    )
    return issues


# --------------------------------------------------------------------------- #


def _title_issues(page: Page) -> list[Issue]:
    assert page.content is not None
    titles = page.content.titles
    url = (page.final_url,)
    if not titles:
        return [
            MISSING_TITLE.issue(
                description="The page has no <title> element.",
                evidence="No <title> element was found in the document.",
                confidence=0.98,
                affected_urls=url,
            )
        ]
    issues = []
    if len(titles) > 1:
        issues.append(
            MULTIPLE_TITLES.issue(
                description=f"The page contains {len(titles)} <title> elements.",
                evidence=f"Found {len(titles)} <title> elements: {quoted_list(titles)}.",
                confidence=0.95,
                affected_urls=url,
                details={"titles": list(titles)},
            )
        )
    title = titles[0]
    if not title:
        issues.append(
            EMPTY_TITLE.issue(
                description="The <title> element exists but contains no text.",
                evidence="<title> element is present but empty.",
                confidence=0.98,
                affected_urls=url,
            )
        )
    elif len(title) < SHORT_TITLE_CHARS:
        issues.append(
            SHORT_TITLE.issue(
                description=f"The title is {len(title)} characters long and may not describe "
                "the page.",
                evidence=f'Title: "{title}" ({len(title)} characters).',
                confidence=0.6,
                affected_urls=url,
                details={"title": title, "length": len(title)},
            )
        )
    elif len(title) > LONG_TITLE_CHARS:
        issues.append(
            LONG_TITLE.issue(
                description=(
                    f"The title is {len(title)} characters long; titles over roughly "
                    f"{LONG_TITLE_CHARS} characters are often truncated in search results."
                ),
                evidence=f'Title: "{title}" ({len(title)} characters).',
                confidence=0.6,
                affected_urls=url,
                details={"title": title, "length": len(title)},
            )
        )
    return issues


def _description_issues(page: Page) -> list[Issue]:
    assert page.content is not None
    descriptions = page.content.meta_descriptions
    url = (page.final_url,)
    if not descriptions:
        return [
            MISSING_DESCRIPTION.issue(
                description="The page has no meta description.",
                evidence='No <meta name="description"> element was found.',
                confidence=0.95,
                affected_urls=url,
            )
        ]
    issues = []
    if len(descriptions) > 1:
        issues.append(
            MULTIPLE_DESCRIPTIONS.issue(
                description=f"The page contains {len(descriptions)} meta description elements.",
                evidence=f"Found {len(descriptions)} meta descriptions: "
                f"{quoted_list(descriptions)}.",
                confidence=0.95,
                affected_urls=url,
            )
        )
    if not descriptions[0]:
        issues.append(
            EMPTY_DESCRIPTION.issue(
                description="The meta description element has no content.",
                evidence='<meta name="description"> is present with empty content.',
                confidence=0.95,
                affected_urls=url,
            )
        )
    return issues


def _canonical_issues(ctx: AuditContext, page: Page) -> list[Issue]:
    """Canonical checks. Precedence: HTML ``<head>`` canonical, then ``Link`` header.

    Canonicals in ``<body>`` (or after an implicit end of ``<head>``) are not used as
    the page's canonical; they are reported as placement problems instead.
    """
    assert page.content is not None
    url = (page.final_url,)
    issues: list[Issue] = issues_for_misplaced_canonicals(page)
    head = [c for c in page.canonical_candidates if c.source == "head"]
    header = list(page.header_canonicals)
    effective = head or header
    if not effective:
        if issues or page.noindex_for_any_agent:
            return issues  # misplaced canonical already reported / noindex pages need none
        return [MISSING_CANONICAL.issue(
            description="No canonical URL is declared for this indexable page.",
            evidence='No <link rel="canonical"> in <head> and no Link: rel="canonical" header.',
            confidence=0.98, affected_urls=url,
        )]  # fmt: skip

    resolved = [normalize_url(c.href, page.final_url) if c.href else None for c in effective]
    if len(head) > 1:
        distinct = {r for r in resolved if r}
        issues.append(MULTIPLE_CANONICALS.issue(
            description=f"The page declares {len(head)} canonical URLs in <head>.",
            evidence=f"Found {len(head)} rel=canonical links: "
                     f"{quoted_list(str(c.href) for c in head)}.",
            confidence=0.95, affected_urls=url,
            severity=Severity.MEDIUM if len(distinct) > 1 else Severity.LOW,
            details={"canonicals": [c.href for c in head]},
        ))  # fmt: skip
    if head and header:
        head_url = normalize_url(head[0].href, page.final_url) if head[0].href else None
        header_url = normalize_url(header[0].href, page.final_url) if header[0].href else None
        if head_url != header_url:
            issues.append(CANONICAL_HEADER_CONFLICT.issue(
                description="The HTML canonical and the HTTP Link header canonical disagree; "
                            "consumers may ignore both.",
                evidence=f"<head>: {head[0].href}; Link header: {header[0].href}",
                confidence=0.9, affected_urls=url,
                details={"html": head[0].href, "header": header[0].href},
            ))  # fmt: skip

    raw, canonical = effective[0].href, resolved[0]
    if canonical is None:
        shown = "no href attribute" if raw is None else f'href="{raw}"'
        issues.append(MALFORMED_CANONICAL.issue(
            description="The canonical link does not resolve to an http(s) URL.",
            evidence=f"rel=canonical ({effective[0].source}) has {shown}.",
            confidence=0.95, affected_urls=url, details={"canonical": raw},
        ))  # fmt: skip
        return issues

    if canonical == page.final_url:
        return issues

    details = {"canonical": canonical, "source": effective[0].source}
    if host_of(canonical) != host_of(page.final_url):
        issues.append(CANONICAL_CROSS_HOST.issue(
            description="The canonical URL is on a different host. This can be intentional.",
            evidence=f"Canonical: {canonical}", confidence=0.95, affected_urls=url, details=details,
        ))  # fmt: skip
    else:
        issues.append(CANONICALIZED.issue(
            description="The page names a different URL as its canonical.",
            evidence=f"Canonical: {canonical}", confidence=0.95, affected_urls=url, details=details,
        ))  # fmt: skip
    if page.noindex_for_any_agent:
        issues.append(CANONICAL_NOINDEX.issue(
            description="The page is marked noindex and also canonicalizes to another URL.",
            evidence=f"Robots directives: {', '.join(sorted(page.directives_for('googlebot')))}; "
                     f"canonical: {canonical}",
            confidence=0.9, affected_urls=url, details=details,
        ))  # fmt: skip

    status = ctx.target_status(canonical)
    if status is None:
        return issues
    if status.error is not None and status.error_kind != "blocked":
        issues.append(
            CANONICAL_TARGET_ERROR.issue(
                description="The canonical URL could not be fetched.",
                evidence=f"Fetching {canonical} failed: {status.error}",
                confidence=0.6,
                affected_urls=url,
                details={**details, "error": status.error},
            )
        )
    elif status.status_code is not None and status.status_code >= 400:
        issues.append(
            CANONICAL_TARGET_ERROR.issue(
                description=f"The canonical URL responds with HTTP {status.status_code}.",
                evidence=f"{canonical} returned HTTP {status.status_code}.",
                confidence=0.9,
                affected_urls=url,
                details={**details, "status": status.status_code},
            )
        )
    elif status.redirected:
        issues.append(
            CANONICAL_TARGET_REDIRECT.issue(
                description="The canonical URL redirects to another URL.",
                evidence=f"{canonical} redirects to {status.final_url}.",
                confidence=0.9,
                affected_urls=url,
                details={**details, "final_url": status.final_url},
            )
        )
    return issues


def _duplicates(
    ctx: AuditContext, rule: Rule, label: str, value_of: Callable[[Page], str | None]
) -> list[Issue]:
    groups: defaultdict[str, list[Page]] = defaultdict(list)
    for page in ctx.pages:
        value = value_of(page)
        if value and not page.noindex_for_any_agent:
            groups[value.strip().casefold()].append(page)

    issues = []
    for pages in groups.values():
        if len(pages) < 2 or _share_one_canonical(pages):
            continue
        urls = [p.final_url for p in pages]
        value = value_of(pages[0])
        issues.append(
            rule.issue(
                description=f"{plural(len(pages), 'page')} share the same {label}.",
                evidence=f'{label.capitalize()} "{value}" is used on: {joined_list(urls)}.',
                confidence=0.97,
                affected_urls=urls,
                details={"value": value, "count": len(pages)},
            )
        )
    return issues


def _share_one_canonical(pages: list[Page]) -> bool:
    """Pages that all canonicalize to one URL are a consolidated set, not duplicates."""
    targets = {
        normalize_url(p.canonical, p.final_url) if p.canonical else p.final_url for p in pages
    }
    return len(targets) == 1
