"""Web Visibility Diagnostic Score - coverage-aware.

An internal engineering diagnostic: how well the crawled site does on the
technical checks *that were actually performed*. It is **not** a prediction of
search rankings, traffic or AI-assistant citations.

Coverage first. Every category records what it could inspect:

* ``scored``      - every applicable item was checked
* ``partial``     - some applicable items were checked (score covers those only)
* ``not-scored``  - applicable items exist but none was checked (e.g. links
  found but none verified, or the crawl failed): **N/A**, and the score is partial
* ``not-applicable`` - there is nothing to check (no images, no links): **N/A**,
  but the audit can still be complete
Neither N/A status is counted in the overall score - never as 100%.

Penalties (per scored category):

    issue_penalty   = SEVERITY_WEIGHT[severity] x confidence x prevalence
    category_points = weight x (1 - min(1, sum of issue penalties))

``prevalence`` is the issue's explicit prevalence when the analyzer knows the
right denominator (e.g. 19 of 20 checked link targets), else affected pages /
audited pages, else 1 for site-level issues. Penalties add up across issues and
rules (only the category floor of 0 caps them), so dozens of failures are never
hidden behind a single capped rule.

Overall = 100 x (points of scored/partial categories) / (their maximum), rounded.
It is ``None`` when no category could be scored.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from web_visibility.analyzers.base import AuditContext
from web_visibility.models import Category, Issue, Severity

SCORE_NAME = "Web Visibility Diagnostic Score"
SCORE_DISCLAIMER = (
    "Diagnostic score for the technical checks that were performed on the crawled pages. "
    "It is not a ranking, traffic or AI-visibility prediction, and no search or "
    "generative engine visibility is guaranteed."
)

CATEGORY_WEIGHTS: dict[Category, int] = {
    Category.TECHNICAL: 25,
    Category.METADATA: 20,
    Category.STRUCTURE: 15,
    Category.LINKS: 20,
    Category.IMAGES: 10,
    Category.STRUCTURED_DATA: 10,
}

SEVERITY_WEIGHTS: dict[Severity, float] = {
    Severity.CRITICAL: 1.0,
    Severity.HIGH: 0.75,
    Severity.MEDIUM: 0.4,
    Severity.LOW: 0.15,
    Severity.INFO: 0.0,
}

CoverageStatus = Literal["scored", "partial", "not-scored", "not-applicable"]
ScoreStatus = Literal["complete", "partial", "not-scored"]


@dataclass(frozen=True, slots=True)
class Coverage:
    status: CoverageStatus
    unit: str
    applicable: int
    """Items the category applies to (discovered and in scope)."""
    checked: int
    failed: int
    """Checked items with at least one non-informational finding."""
    reason: str | None = None

    @property
    def ratio(self) -> float | None:
        return None if self.applicable == 0 else self.checked / self.applicable


@dataclass(frozen=True, slots=True)
class Deduction:
    rule_id: str
    severity: Severity
    issues: int
    penalty: float
    """Fraction of the category (0-1) this rule costs."""
    points: float


@dataclass(frozen=True, slots=True)
class CategoryScore:
    category: Category
    max_points: int
    points: float | None
    """``None`` when the category is not scored (N/A)."""
    coverage: Coverage
    deductions: tuple[Deduction, ...]

    @property
    def scored(self) -> bool:
        return self.points is not None


@dataclass(frozen=True, slots=True)
class Score:
    overall: int | None
    categories: tuple[CategoryScore, ...]
    pages_scored: int
    status: ScoreStatus
    scored_weight: int
    """Sum of the weights of categories that contributed to ``overall`` (max 100)."""

    @property
    def scored(self) -> bool:
        return self.overall is not None


def measure_coverage(ctx: AuditContext, issues: list[Issue]) -> dict[Category, Coverage]:
    """What each category could inspect on this crawl."""
    pages = len(ctx.pages)
    shells = sum(1 for p in ctx.pages if p.likely_client_rendered)
    pending = len(ctx.crawl.pending)
    failed = _failed_items(issues)
    coverage: dict[Category, Coverage] = {}

    if pages == 0:
        reason = "no page could be audited (" + "; ".join(ctx.crawl.state_reasons) + ")"
        for category in CATEGORY_WEIGHTS:
            coverage[category] = Coverage("not-scored", "pages", 0, 0, 0, reason)
        return coverage

    crawled = len(ctx.crawl.pages)
    tech_partial = bool(pending or ctx.crawl.stop_reason)
    coverage[Category.TECHNICAL] = Coverage(
        "partial" if tech_partial else "scored",
        "URLs",
        crawled + pending,
        crawled,
        failed[Category.TECHNICAL],
        _pending_reason(ctx) if tech_partial else None,
    )

    page_reason = None
    if shells:
        page_reason = f"{shells} of {pages} pages appear client-rendered (JavaScript not run)"
    elif pending:
        page_reason = _pending_reason(ctx)
    for category in (Category.METADATA, Category.STRUCTURE, Category.STRUCTURED_DATA):
        coverage[category] = Coverage(
            "partial" if page_reason else "scored",
            "pages",
            pages + pending,
            pages - shells,
            failed[category],
            page_reason,
        )

    coverage[Category.LINKS] = _link_coverage(ctx, failed[Category.LINKS])

    image_count = sum(len(p.images) for p in ctx.pages)
    if image_count == 0:
        coverage[Category.IMAGES] = Coverage(
            "not-applicable", "images", 0, 0, 0, "no images on the audited pages"
        )
    else:
        coverage[Category.IMAGES] = Coverage(
            "partial" if pending else "scored",
            "images",
            image_count,
            image_count,
            failed[Category.IMAGES],
            _pending_reason(ctx) if pending else None,
        )
    return coverage


def compute_score(
    issues: list[Issue], coverage: dict[Category, Coverage], pages_audited: int
) -> Score:
    by_category: defaultdict[Category, defaultdict[str, list[Issue]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for issue in issues:
        by_category[issue.category][issue.id].append(issue)

    categories = []
    for category, weight in CATEGORY_WEIGHTS.items():
        cov = coverage[category]
        deductions = []
        for rule_id, rule_issues in sorted(by_category[category].items()):
            penalty = _rule_penalty(rule_issues, pages_audited)
            if penalty <= 0:
                continue
            deductions.append(
                Deduction(
                    rule_id=rule_id,
                    severity=min((i.severity for i in rule_issues), key=lambda s: s.rank),
                    issues=len(rule_issues),
                    penalty=round(penalty, 4),
                    points=round(weight * penalty, 2),
                )
            )
        deductions.sort(key=lambda d: -d.points)
        points = None
        if cov.status in ("scored", "partial"):
            total_penalty = min(1.0, sum(d.penalty for d in deductions))
            points = round(weight * (1.0 - total_penalty), 1)
        categories.append(CategoryScore(category, weight, points, cov, tuple(deductions)))

    scored = [c for c in categories if c.points is not None]
    scored_weight = sum(c.max_points for c in scored)
    if not scored:
        return Score(None, tuple(categories), pages_audited, "not-scored", 0)
    overall = round(100 * sum(c.points or 0.0 for c in scored) / scored_weight)
    complete = all(c.coverage.status in ("scored", "not-applicable") for c in categories)
    return Score(
        overall, tuple(categories), pages_audited, "complete" if complete else "partial",
        scored_weight,
    )  # fmt: skip


def prevalence_of(issue: Issue, pages_audited: int) -> float:
    if issue.prevalence is not None:
        return issue.prevalence
    if issue.site_level:
        return 1.0
    return min(1.0, len(issue.affected_urls) / max(1, pages_audited))


def _rule_penalty(issues: list[Issue], pages_audited: int) -> float:
    total = sum(
        SEVERITY_WEIGHTS[i.severity] * i.confidence * prevalence_of(i, pages_audited)
        for i in issues
    )
    return min(1.0, total)


def _link_coverage(ctx: AuditContext, failed: int) -> Coverage:
    links = ctx.link_coverage()
    external = ctx.crawl.config.check_external
    applicable = links.internal_discovered + (links.external_discovered if external else 0)
    checked = links.internal_checked + (links.external_checked if external else 0)
    if applicable == 0:
        return Coverage("not-applicable", "link targets", 0, 0, 0, "no links to other pages found")
    if checked == 0:
        reason = "link validation was not performed (no link target was checked)"
        return Coverage("not-scored", "link targets", applicable, 0, 0, reason)
    if checked < applicable:
        reason = f"{applicable - checked} of {applicable} link targets unchecked"
        return Coverage("partial", "link targets", applicable, checked, failed, reason)
    return Coverage("scored", "link targets", applicable, checked, failed)


def _failed_items(issues: list[Issue]) -> dict[Category, int]:
    affected: defaultdict[Category, set[str]] = defaultdict(set)
    for issue in issues:
        if issue.severity is Severity.INFO:
            continue
        if issue.category is Category.LINKS:
            # Links coverage is counted in link targets; page-level link findings
            # (orphans, empty anchors) are not target failures.
            if "target" in issue.details:
                affected[issue.category].add(str(issue.details["target"]))
        else:
            affected[issue.category].update(issue.affected_urls or ("(site)",))
    return {category: len(affected[category]) for category in CATEGORY_WEIGHTS}


def _pending_reason(ctx: AuditContext) -> str:
    if ctx.crawl.stop_reason:
        return f"crawl stopped early ({ctx.crawl.stop_reason})"
    return f"{len(ctx.crawl.pending)} discovered URL(s) not crawled (page limit)"
