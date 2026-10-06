"""Heading structure: H1 presence, multiple H1s, skipped levels, empty headings.

These are reported as structural conditions. Multiple H1s, for example, are
valid HTML and not inherently harmful; the finding exists so an agent can
decide whether the outline matches the page's intent.
"""

from __future__ import annotations

from web_visibility.analyzers.base import AuditContext, plural, quoted_list
from web_visibility.models import Category, Issue, Page, Rule, Severity

C = Category.STRUCTURE

MISSING_H1 = Rule(
    "missing-h1",
    C,
    Severity.MEDIUM,
    "Missing H1 heading",
    "Add one H1 that states the page's main topic.",
)
MULTIPLE_H1 = Rule(
    "multiple-h1",
    C,
    Severity.LOW,
    "Multiple H1 headings",
    "Check that the H1s reflect the page's main topic; a single H1 usually gives the clearest "
    "outline, but multiple H1s are valid HTML.",
)
HEADING_SKIP = Rule(
    "heading-level-skip",
    C,
    Severity.LOW,
    "Skipped heading levels",
    "Nest headings without skipping levels (H2 then H3, not H2 then H4) so the outline is "
    "unambiguous to assistive technology and parsers.",
)
EMPTY_HEADING = Rule(
    "empty-heading",
    C,
    Severity.LOW,
    "Empty heading elements",
    "Remove empty heading elements or give them text; don't use headings for spacing.",
)

RULES = (MISSING_H1, MULTIPLE_H1, HEADING_SKIP, EMPTY_HEADING)


def analyze(ctx: AuditContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        issues += _page_issues(page)
    return issues


def _page_issues(page: Page) -> list[Issue]:
    url = (page.final_url,)
    headings = page.headings
    h1s = [h for h in headings if h.level == 1]
    issues: list[Issue] = []

    if not h1s:
        found = ", ".join(f"H{h.level}" for h in headings[:5]) or "none"
        issues.append(
            MISSING_H1.issue(
                description="The page has no H1 element.",
                evidence=f"No <h1> element found. Headings present: {found}.",
                confidence=0.95,
                affected_urls=url,
            )
        )
    elif len(h1s) > 1:
        issues.append(
            MULTIPLE_H1.issue(
                description=f"The page has {len(h1s)} H1 elements.",
                evidence=f"H1 texts: {quoted_list(h.text for h in h1s)}.",
                confidence=0.95,
                affected_urls=url,
                details={"h1s": [h.text for h in h1s]},
            )
        )

    skips = []
    previous = 0
    for heading in headings:
        if previous and heading.level > previous + 1:
            skips.append(f'H{previous} -> H{heading.level} ("{heading.text[:60]}")')
        previous = heading.level
    if skips:
        issues.append(
            HEADING_SKIP.issue(
                description=f"Heading levels are skipped {plural(len(skips), 'time')}.",
                evidence="; ".join(skips[:5])
                + (f" (+{len(skips) - 5} more)" if len(skips) > 5 else ""),
                confidence=0.9,
                affected_urls=url,
                details={"skips": skips},
            )
        )

    empty = [h for h in headings if not h.text]
    if empty:
        levels = ", ".join(f"H{h.level}" for h in empty[:10])
        issues.append(
            EMPTY_HEADING.issue(
                description=f"{plural(len(empty), 'heading element')} contain no text.",
                evidence=f"Empty headings: {levels}.",
                confidence=0.9,
                affected_urls=url,
                details={"count": len(empty)},
            )
        )
    return issues
