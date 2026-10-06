"""Basic JSON-LD structured data checks (Phase 01 scope).

Detects, extracts and parses JSON-LD, identifies top-level ``@type`` values and
flags syntactically broken blocks. It does **not** validate blocks against
Schema.org vocabularies or rich-result requirements; that is Phase 02. It also
never suggests adding a schema type, because whether a type is *supported by
the page's actual content* requires judgement this deterministic layer lacks.
"""

from __future__ import annotations

from collections import Counter

from web_visibility.analyzers.base import AuditContext, joined_list, plural
from web_visibility.models import Category, Issue, Page, Rule, Severity

C = Category.STRUCTURED_DATA

INVALID_JSON_LD = Rule(
    "invalid-json-ld",
    C,
    Severity.MEDIUM,
    "Invalid JSON-LD block",
    "Fix the JSON syntax; consumers typically ignore blocks that fail to parse.",
)
MISSING_CONTEXT = Rule(
    "json-ld-missing-context",
    C,
    Severity.LOW,
    "JSON-LD without @context",
    'Add "@context": "https://schema.org" so the vocabulary is unambiguous.',
)
MISSING_TYPE = Rule(
    "json-ld-missing-type",
    C,
    Severity.LOW,
    "JSON-LD entity without @type",
    "Give each top-level entity an @type describing what it is.",
)
NO_STRUCTURED_DATA = Rule(
    "no-structured-data",
    C,
    Severity.LOW,
    "No JSON-LD structured data found",
    "Consider adding JSON-LD that describes information actually present on the page "
    "(for example the organization behind the site). Never mark up content that is not "
    "visible or supported.",
)
OTHER_FORMATS = Rule(
    "non-json-ld-structured-data",
    C,
    Severity.INFO,
    "Microdata or RDFa detected (not analyzed)",
    "No action needed; Phase 01 only parses JSON-LD, so these annotations were not validated.",
)

RULES = (INVALID_JSON_LD, MISSING_CONTEXT, MISSING_TYPE, NO_STRUCTURED_DATA, OTHER_FORMATS)


def analyze(ctx: AuditContext) -> list[Issue]:
    if not ctx.pages:
        return []
    issues: list[Issue] = []
    for page in ctx.pages:
        issues += _page_issues(page)

    with_json_ld = [p for p in ctx.pages if p.structured_data]
    with_other = [
        p.final_url
        for p in ctx.pages
        if p.content is not None and (p.content.has_microdata or p.content.has_rdfa)
    ]
    if with_other:
        issues.append(
            OTHER_FORMATS.issue(
                description=f"{plural(len(with_other), 'page')} contain microdata or RDFa "
                "attributes.",
                evidence=f"itemscope/typeof attributes found on: {joined_list(with_other)}",
                confidence=0.9,
                affected_urls=with_other,
            )
        )
    if not with_json_ld and not with_other:
        issues.append(
            NO_STRUCTURED_DATA.issue(
                description="None of the crawled pages contain JSON-LD, microdata or RDFa.",
                evidence=f"0 of {ctx.page_count} crawled pages contain a "
                '<script type="application/ld+json"> block.',
                confidence=0.9 if not ctx.partial else 0.7,
            )
        )
    return issues


def type_inventory(ctx: AuditContext) -> dict[str, int]:
    """How many pages declare each top-level ``@type`` (for reports)."""
    counts: Counter[str] = Counter()
    for page in ctx.pages:
        counts.update({t for block in page.structured_data for t in block.types})
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _page_issues(page: Page) -> list[Issue]:
    url = (page.final_url,)
    blocks = page.structured_data
    issues: list[Issue] = []

    invalid = [(i, b) for i, b in enumerate(blocks, 1) if not b.valid]
    if invalid:
        issues.append(
            INVALID_JSON_LD.issue(
                description=f"{plural(len(invalid), 'JSON-LD block')} of {len(blocks)} could "
                "not be parsed.",
                evidence="; ".join(f"block {i}: {b.error}" for i, b in invalid[:5]),
                confidence=0.97,
                affected_urls=url,
                details={
                    "errors": [b.error for _, b in invalid],
                    "snippets": [b.raw[:200] for _, b in invalid],
                },
            )
        )

    no_context = [i for i, b in enumerate(blocks, 1) if b.valid and b.missing_context]
    if no_context:
        issues.append(
            MISSING_CONTEXT.issue(
                description="JSON-LD is present without an @context declaration.",
                evidence=f"No @context in block(s) {', '.join(map(str, no_context))}.",
                confidence=0.9,
                affected_urls=url,
            )
        )

    no_type = [i for i, b in enumerate(blocks, 1) if b.valid and b.missing_type]
    if no_type:
        issues.append(
            MISSING_TYPE.issue(
                description="A JSON-LD entity has no @type.",
                evidence=f"Entity without @type in block(s) {', '.join(map(str, no_type))}.",
                confidence=0.9,
                affected_urls=url,
            )
        )
    return issues
