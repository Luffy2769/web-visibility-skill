"""Issues (findings) and the rules that produce them."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """How much attention an issue deserves. Used sparingly at the top end."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Sort key: 0 is most severe."""
        return _SEVERITY_ORDER.index(self)


_SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO]


class Category(StrEnum):
    """Scoring categories."""

    TECHNICAL = "technical"
    METADATA = "metadata"
    STRUCTURE = "structure"
    LINKS = "links"
    IMAGES = "images"
    STRUCTURED_DATA = "structured_data"

    @property
    def label(self) -> str:
        return _CATEGORY_LABELS[self]


_CATEGORY_LABELS = {
    Category.TECHNICAL: "Technical SEO",
    Category.METADATA: "Metadata",
    Category.STRUCTURE: "Headings / Structure",
    Category.LINKS: "Links",
    Category.IMAGES: "Images",
    Category.STRUCTURED_DATA: "Structured Data",
}


@dataclass(frozen=True, slots=True)
class Issue:
    """An evidence-backed finding.

    ``affected_urls`` lists the pages where the condition was observed (and where
    a fix would be made). It is empty for site-level findings such as a missing
    robots.txt. ``details`` carries machine-readable specifics for agents.

    ``prevalence`` (0-1) optionally overrides how widespread the issue is for
    scoring, when the analyzer knows a better denominator than "audited pages"
    (e.g. 2 of 40 sitemap entries, or 3 of 30 checked link targets).

    ``impact`` and ``effort`` (1-10) are part of the project's priority model but
    are only set when an analyzer can justify them; Phase 01 analyzers leave them
    ``None`` rather than guess. ``verification`` says how to confirm a fix.
    """

    id: str
    category: Category
    severity: Severity
    title: str
    description: str
    evidence: str
    recommendation: str
    confidence: float
    affected_urls: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)
    prevalence: float | None = None
    impact: int | None = None
    effort: int | None = None
    verification: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within [0, 1], got {self.confidence}")
        if self.prevalence is not None and not 0.0 <= self.prevalence <= 1.0:
            raise ValueError(f"prevalence must be within [0, 1], got {self.prevalence}")
        for name in ("impact", "effort"):
            value = getattr(self, name)
            if value is not None and not 1 <= value <= 10:
                raise ValueError(f"{name} must be within [1, 10], got {value}")
        if not self.evidence.strip():
            raise ValueError(f"issue {self.id!r} has no evidence")

    @property
    def affected_url(self) -> str | None:
        """The primary affected URL, if any."""
        return self.affected_urls[0] if self.affected_urls else None

    @property
    def site_level(self) -> bool:
        return not self.affected_urls


@dataclass(frozen=True, slots=True)
class Rule:
    """A check's fixed metadata. Analyzers create issues through their rules so
    titles, categories and recommendations stay consistent across the codebase."""

    id: str
    category: Category
    severity: Severity
    title: str
    recommendation: str
    verification: str | None = None
    """How to confirm a fix; defaults to re-running the audit for this rule."""

    def issue(
        self,
        *,
        description: str,
        evidence: str,
        confidence: float,
        affected_urls: tuple[str, ...] | list[str] = (),
        details: dict[str, Any] | None = None,
        severity: Severity | None = None,
        recommendation: str | None = None,
        prevalence: float | None = None,
    ) -> Issue:
        return Issue(
            id=self.id,
            category=self.category,
            severity=severity or self.severity,
            title=self.title,
            description=description,
            evidence=evidence,
            recommendation=recommendation or self.recommendation,
            confidence=round(confidence, 3),
            affected_urls=tuple(affected_urls),
            details=details or {},
            prevalence=None if prevalence is None else round(min(1.0, prevalence), 4),
            verification=self.verification
            or f"Re-run the audit and confirm `{self.id}` is no longer reported"
            + (" for the affected URLs." if affected_urls else "."),
        )
