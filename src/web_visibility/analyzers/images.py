"""Image alt attributes and explicit dimensions.

A *missing* ``alt`` attribute and an *empty* ``alt=""`` are different things:
``alt=""`` is the correct markup for decorative images, so it is reported only
as informational context, never as a defect.
"""

from __future__ import annotations

from web_visibility.analyzers.base import AuditContext, joined_list, plural, sample
from web_visibility.models import Category, Issue, Page, Rule, Severity

C = Category.IMAGES

MISSING_ALT = Rule(
    "image-missing-alt",
    C,
    Severity.MEDIUM,
    "Images without an alt attribute",
    'Add alt text describing each informative image; use alt="" for purely decorative ones.',
)
EMPTY_ALT = Rule(
    "image-empty-alt",
    C,
    Severity.INFO,
    'Images marked as decorative (alt="")',
    "No action needed if these images are decorative; otherwise describe them in alt text.",
)
MISSING_DIMENSIONS = Rule(
    "image-missing-dimensions",
    C,
    Severity.LOW,
    "Images without width/height attributes",
    "Set width and height attributes (or a CSS aspect-ratio) so the browser can reserve space "
    "and avoid layout shift.",
)

RULES = (MISSING_ALT, EMPTY_ALT, MISSING_DIMENSIONS)


def analyze(ctx: AuditContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        issues += _page_issues(page)
    return issues


def _page_issues(page: Page) -> list[Issue]:
    url = (page.final_url,)
    images = page.images
    if not images:
        return []
    issues: list[Issue] = []

    missing = [img for img in images if img.alt is None]
    if missing:
        srcs = [img.src or "(no src)" for img in missing]
        issues.append(
            MISSING_ALT.issue(
                description=f"{len(missing)} of {len(images)} images on this page have no "
                "alt attribute.",
                evidence=f"Missing alt: {joined_list(srcs)}",
                confidence=0.97,
                affected_urls=url,
                details={
                    "count": len(missing),
                    "total_images": len(images),
                    "images": sample(srcs, 20),
                },
            )
        )

    empty = [img for img in images if img.alt is not None and not img.alt.strip()]
    if empty:
        srcs = [img.src or "(no src)" for img in empty]
        issues.append(
            EMPTY_ALT.issue(
                description=f'{plural(len(empty), "image")} use alt="" (treated as decorative).',
                evidence=f'alt="": {joined_list(srcs)}',
                confidence=0.9,
                affected_urls=url,
                details={"count": len(empty), "images": sample(srcs, 20)},
            )
        )

    unsized = [img for img in images if not img.width or not img.height]
    if unsized:
        srcs = [img.src or "(no src)" for img in unsized]
        issues.append(
            MISSING_DIMENSIONS.issue(
                description=f"{plural(len(unsized), 'image')} lack width and/or height attributes. "
                "They may still be sized by CSS, which this audit does not evaluate.",
                evidence=f"No width/height attributes: {joined_list(srcs)}",
                confidence=0.6,
                affected_urls=url,
                details={"count": len(unsized), "images": sample(srcs, 20)},
            )
        )
    return issues
