"""The audit pipeline: crawl -> analyze -> measure coverage -> score.

This is the single entry point used by the CLI, the tests and (eventually) any
other front end. It performs no printing and no file I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from web_visibility import REPORT_SCHEMA_VERSION, __version__
from web_visibility.analyzers import AuditContext, run_analyzers
from web_visibility.analyzers.schema import type_inventory
from web_visibility.config import CrawlConfig
from web_visibility.crawler import Crawler, ProgressCallback
from web_visibility.fetch import Fetcher
from web_visibility.models import Category, CrawlResult, Issue
from web_visibility.scoring import Coverage, Score, compute_score, measure_coverage


@dataclass(frozen=True)
class AuditReport:
    target_url: str
    """The URL exactly as the user supplied it."""
    context: AuditContext
    issues: tuple[Issue, ...]
    score: Score
    coverage: dict[Category, Coverage]
    structured_data_types: dict[str, int]
    generated_at: str
    version: str = __version__
    schema_version: str = REPORT_SCHEMA_VERSION

    @property
    def crawl(self) -> CrawlResult:
        return self.context.crawl

    @property
    def pages_audited(self) -> int:
        return len(self.context.pages)

    @property
    def auditable(self) -> bool:
        """True when at least one page could be audited (the crawl did not fail)."""
        return self.crawl.state != "failed"


def build_report(url: str, crawl: CrawlResult) -> AuditReport:
    """Analyze and score an existing crawl result."""
    ctx = AuditContext.build(crawl)
    issues = run_analyzers(ctx)
    coverage = measure_coverage(ctx, issues)
    return AuditReport(
        target_url=url,
        context=ctx,
        issues=tuple(issues),
        score=compute_score(issues, coverage, ctx.page_count),
        coverage=coverage,
        structured_data_types=type_inventory(ctx),
        generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
    )


def run_audit(
    url: str,
    config: CrawlConfig | None = None,
    *,
    fetcher: Fetcher | None = None,
    progress: ProgressCallback | None = None,
) -> AuditReport:
    """Crawl ``url`` and produce an evidence-backed report.

    Raises :class:`~web_visibility.crawler.InvalidStartURLError` for a malformed
    URL. Network problems never raise; they become findings and crawl states.
    """
    config = config or CrawlConfig()
    crawl = Crawler(config, fetcher=fetcher, progress=progress).crawl(url)
    return build_report(url, crawl)
