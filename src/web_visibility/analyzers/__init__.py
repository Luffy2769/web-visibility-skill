"""Deterministic analyzers.

Each analyzer module exposes ``analyze(ctx) -> list[Issue]`` and a ``RULES``
tuple describing every check it can report. Adding an analyzer means adding a
module and registering it in ``_MODULES`` - nothing else changes.
"""

from __future__ import annotations

from dataclasses import replace
from types import ModuleType

from web_visibility.analyzers import (
    crawlability,
    headings,
    images,
    links,
    metadata,
    schema,
    technical,
)
from web_visibility.analyzers.base import Analyzer, AuditContext
from web_visibility.models import Issue, Rule

_MODULES: tuple[ModuleType, ...] = (
    technical,
    crawlability,
    metadata,
    headings,
    links,
    images,
    schema,
)

ANALYZERS: tuple[Analyzer, ...] = tuple(module.analyze for module in _MODULES)

#: Findings about something being *absent* that browser rendering could add.
RENDER_DEPENDENT_RULES = frozenset(
    {
        "missing-title",
        "empty-title",
        "short-title",
        "missing-meta-description",
        "missing-canonical",
        "missing-h1",
        "no-internal-outlinks",
        "duplicate-title",
        "duplicate-meta-description",
        "no-structured-data",
        "potential-orphan-page",
    }
)
#: Confidence cap for render-dependent findings on client-rendered pages.
RENDER_DEPENDENT_CONFIDENCE = 0.35
# Site-level findings whose evidence can come from any page.
_SITE_WIDE_RENDER_RULES = frozenset({"no-structured-data", "potential-orphan-page"})


def run_analyzers(ctx: AuditContext) -> list[Issue]:
    """Run every registered analyzer and return issues, most severe first."""
    issues: list[Issue] = []
    for analyze in ANALYZERS:
        issues.extend(analyze(ctx))
    issues = [_hedge_for_rendering(ctx, issue) for issue in issues]
    return sorted(issues, key=lambda i: (i.severity.rank, -len(i.affected_urls), i.id))


def _hedge_for_rendering(ctx: AuditContext, issue: Issue) -> Issue:
    """Lower confidence of "missing X" findings that JavaScript rendering could explain.

    A client-rendered shell's server HTML lacks headings, links and often metadata
    that the browser adds later. Such findings are kept (they are true of the
    server response) but capped at ``RENDER_DEPENDENT_CONFIDENCE`` and marked.
    """
    if issue.id not in RENDER_DEPENDENT_RULES:
        return issue
    shells = {p.final_url for p in ctx.pages if p.likely_client_rendered}
    if not shells:
        return issue
    if issue.id in _SITE_WIDE_RENDER_RULES or issue.site_level:
        applies = True
    else:
        applies = all(url in shells for url in issue.affected_urls)
    if not applies:
        return issue
    return replace(
        issue,
        confidence=min(issue.confidence, RENDER_DEPENDENT_CONFIDENCE),
        description=issue.description
        + " (Based on the server HTML only: the page appears client-rendered, so the "
        "browser-rendered page may differ.)",
        details={**issue.details, "render_dependent": True},
    )


def all_rules() -> tuple[Rule, ...]:
    """Every rule any analyzer can emit (used by documentation tests)."""
    rules: list[Rule] = []
    for module in _MODULES:
        rules.extend(module.RULES)
    return tuple(rules)


__all__ = [
    "ANALYZERS",
    "RENDER_DEPENDENT_CONFIDENCE",
    "RENDER_DEPENDENT_RULES",
    "AuditContext",
    "all_rules",
    "run_analyzers",
]
