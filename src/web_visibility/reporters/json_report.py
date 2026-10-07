"""Machine-readable JSON report.

The structure is documented field-by-field in docs/report-format.md and is
versioned by ``schema_version`` (``web_visibility.REPORT_SCHEMA_VERSION``).
Versioning policy: additive changes bump the minor number; renames, removals or
changed meanings bump the major number. Consumers must check the major number.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from web_visibility import REPORT_SCHEMA_VERSION
from web_visibility.analyzers.crawlability import crawler_access_summary
from web_visibility.audit import AuditReport
from web_visibility.models import INDEXING_AGENTS, Category, Issue, Page, Severity, SitemapFile
from web_visibility.scoring import (
    MIN_SCORED_WEIGHT,
    SCORE_DISCLAIMER,
    SCORE_NAME,
    SEVERITY_WEIGHTS,
)

SCHEMA_VERSION = REPORT_SCHEMA_VERSION
MAX_SITEMAP_URLS_IN_REPORT = 1000

LIMITATIONS = (
    "Phase 01 covers technical checks only; content quality, GEO, AEO, entity and authority "
    "analysis are not implemented and are not scored.",
    "Pages are fetched without executing JavaScript. Pages that look like client-rendered "
    "shells are flagged and their 'missing content' findings carry reduced confidence.",
    "robots.txt is evaluated for a fixed list of crawler tokens; engine-specific fallbacks "
    "(e.g. googlebot-image -> googlebot) are not modelled.",
    "Structured data is checked for JSON-LD syntax and @type only, not against Schema.org "
    "or rich-result requirements.",
    "Only the crawled pages are analyzed; site-wide conclusions are limited to that subset.",
    "Findings describe observed conditions. They do not predict rankings or traffic.",
)


def build_report(report: AuditReport) -> dict[str, Any]:
    crawl = report.crawl
    issues_by_url: defaultdict[str, list[str]] = defaultdict(list)
    for issue in report.issues:
        for url in issue.affected_urls:
            if issue.id not in issues_by_url[url]:
                issues_by_url[url].append(issue.id)

    ctx = report.context
    audited = {p.final_url for p in ctx.pages}

    return {
        "schema_version": SCHEMA_VERSION,
        "project": "web-visibility",
        "version": report.version,
        "generated_at": report.generated_at,
        "target": {
            "url": report.target_url,
            "normalized_url": crawl.start_url,
            "final_url": ctx.start_page.final_url if ctx.start_page else None,
        },
        "crawl": _crawl_section(report),
        "score": _score_section(report),
        "summary": _summary_section(report.issues),
        "issues": [_issue(issue) for issue in report.issues],
        "pages": [
            _page(
                page,
                ctx.inbound_sources(page),
                page.final_url in audited,
                issues_by_url.get(page.final_url, []) or issues_by_url.get(page.requested_url, []),
            )
            for page in crawl.pages
        ],
        "robots": _robots_section(report),
        "sitemap": _sitemap_section(report),
        "structured_data": {"types": report.structured_data_types},
        "limitations": list(LIMITATIONS),
    }


def to_json(report: AuditReport, *, indent: int | None = 2) -> str:
    return json.dumps(build_report(report), indent=indent, ensure_ascii=False)


def write_json(report: AuditReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(report) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #


def _crawl_section(report: AuditReport) -> dict[str, Any]:
    crawl = report.crawl
    config = crawl.config
    return {
        "state": crawl.state,
        "state_reasons": list(crawl.state_reasons),
        "start_url": crawl.start_url,
        "site_url": crawl.site_url,
        "pages_crawled": len(crawl.pages),
        "pages_audited": report.pages_audited,
        "pages_unretrieved": len(crawl.unretrieved_pages),
        "unretrieved": [
            {"url": p.requested_url, "reason": p.unretrieved_reason, "status_code": p.status_code}
            for p in crawl.unretrieved_pages[:50]
        ],
        "pages_client_rendered": sum(1 for p in report.context.pages if p.likely_client_rendered),
        "max_pages": config.max_pages,
        "limit_reached": crawl.limit_reached,
        "stop_reason": crawl.stop_reason,
        "crawl_delay_seconds": crawl.crawl_delay,
        "pending_urls": len(crawl.pending),
        "pending_sample": list(crawl.pending[:20]),
        "skipped": [{"url": url, "reason": reason} for url, reason in crawl.skipped.items()],
        "links_checked": len(crawl.link_checks),
        "links_unchecked": len(crawl.unchecked_links),
        "requests": crawl.request_count,
        "duration_seconds": crawl.duration_seconds,
        "settings": {
            "respect_robots": config.respect_robots,
            "check_external": config.check_external,
            "max_link_checks": config.max_link_checks,
            "max_external_checks": config.max_external_checks,
            "timeout_seconds": config.fetch.timeout,
            "delay_seconds": config.fetch.delay,
            "max_page_bytes": config.fetch.max_bytes,
            "max_redirects": config.fetch.max_redirects,
            "max_retry_wait_seconds": config.fetch.max_retry_wait,
            "max_crawl_delay_seconds": config.max_crawl_delay,
            "allow_private_network": config.fetch.allow_private,
            "user_agent": config.fetch.user_agent,
        },
    }


def _score_section(report: AuditReport) -> dict[str, Any]:
    score = report.score
    return {
        "name": SCORE_NAME,
        "status": score.status,
        "overall": score.overall,
        "max": 100,
        "scored_weight": score.scored_weight,
        "min_scored_weight": MIN_SCORED_WEIGHT,
        "reason": score.reason,
        "disclaimer": SCORE_DISCLAIMER,
        "categories": {
            c.category.value: {
                "label": c.category.label,
                "score": c.points,
                "max": c.max_points,
                "coverage": {
                    "status": c.coverage.status,
                    "unit": c.coverage.unit,
                    "applicable": c.coverage.applicable,
                    "checked": c.coverage.checked,
                    "failed": c.coverage.failed,
                    "reason": c.coverage.reason,
                },
                "deductions": [
                    {
                        "rule_id": d.rule_id,
                        "severity": d.severity.value,
                        "issues": d.issues,
                        "penalty": d.penalty,
                        "points": d.points,
                    }
                    for d in c.deductions
                ],
            }
            for c in score.categories
        },
        "model": {
            "severity_weights": {s.value: w for s, w in SEVERITY_WEIGHTS.items()},
            "formula": "category = weight * (1 - min(1, sum(severity_weight * confidence * "
            "prevalence))); overall = 100 * sum(scored points) / sum(scored weights); "
            "not-scored categories are excluded; overall is withheld "
            "(status insufficient-coverage) when scored_weight < min_scored_weight",
        },
    }


def _summary_section(issues: tuple[Issue, ...]) -> dict[str, Any]:
    by_severity = {s.value: 0 for s in Severity}
    by_category = {c.value: 0 for c in Category}
    for issue in issues:
        by_severity[issue.severity.value] += 1
        by_category[issue.category.value] += 1
    return {"issues_total": len(issues), "by_severity": by_severity, "by_category": by_category}


def _issue(issue: Issue) -> dict[str, Any]:
    return {
        "id": issue.id,
        "category": issue.category.value,
        "severity": issue.severity.value,
        "title": issue.title,
        "description": issue.description,
        "evidence": issue.evidence,
        "affected_url": issue.affected_url,
        "affected_urls": list(issue.affected_urls),
        "recommendation": issue.recommendation,
        "verification": issue.verification,
        "confidence": issue.confidence,
        "prevalence": issue.prevalence,
        "impact": issue.impact,
        "effort": issue.effort,
        "details": issue.details,
    }


def _page(
    page: Page, inbound: frozenset[str], audited: bool, issue_ids: list[str]
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "url": page.final_url,
        "requested_url": page.requested_url,
        "status_code": page.status_code,
        "content_type": page.content_type,
        "discovered_via": page.discovered_via.value,
        "depth": page.depth,
        "audited": audited,
        "redirect_chain": [
            {"url": h.url, "status_code": h.status_code} for h in page.redirect_chain
        ],
        "error": page.error,
        "error_kind": page.error_kind,
        "truncated": page.truncated,
        "response_headers": page.response_headers,
        "issue_ids": issue_ids,
    }
    if page.content is None:
        return data
    content = page.content
    internal = page.internal_links
    data.update({
        "title": page.title,
        "meta_description": page.meta_description,
        "canonical": page.canonical,
        "canonicals": [{"href": c.href, "source": c.source} for c in page.canonical_candidates],
        "robots_meta": [{"name": m.name, "content": m.content} for m in content.robots_meta],
        "x_robots_tag": page.x_robots_tag,
        "noindex": page.is_noindex,
        "robots_directives": {
            "all": sorted(page.robots_directives),
            **{agent: sorted(page.directives_for(agent)) for agent in INDEXING_AGENTS},
        },
        "lang": content.lang,
        "rendering": {
            "likely_client_rendered": content.likely_client_rendered,
            "visible_text_length": content.visible_text_length,
            "signals": list(content.render_signals),
        },
        "head_anomalies": list(content.head_anomalies),
        "h1s": list(page.h1s),
        "headings": [{"level": h.level, "text": h.text} for h in page.headings],
        "links": {
            "internal": len(internal),
            "internal_unique": len({link.url for link in internal}),
            "external": len(page.external_links),
            "nofollow": sum(1 for link in page.links if link.nofollow),
            "internal_targets": list(dict.fromkeys(link.url for link in internal)),
            "external_targets": list(dict.fromkeys(link.url for link in page.external_links)),
        },
        "inbound_internal_links": len(inbound),
        "images": {
            "total": len(page.images),
            "missing_alt": sum(1 for i in page.images if i.alt is None),
            "empty_alt": sum(1 for i in page.images if i.alt is not None and not i.alt.strip()),
            "missing_dimensions": sum(1 for i in page.images if not i.width or not i.height),
        },
        "structured_data": [
            {"types": list(block.types), "valid": block.valid, "error": block.error}
            for block in page.structured_data
        ],
        "has_microdata": content.has_microdata,
        "has_rdfa": content.has_rdfa,
    })  # fmt: skip
    return data


def _robots_section(report: AuditReport) -> dict[str, Any]:
    robots = report.crawl.robots
    rules = robots.rules
    return {
        "url": robots.url,
        "state": robots.state,
        "exists": robots.exists,
        "restricts_crawling": robots.restricts_crawling,
        "status_code": robots.status_code,
        "content_type": robots.content_type,
        "error": robots.error,
        "sitemaps": list(rules.sitemaps) if rules else [],
        "crawlers": crawler_access_summary(report.context),
        "groups": [
            {
                "user_agents": list(group.user_agents),
                "rules": [{"allow": r.allow, "path": r.raw or r.path} for r in group.rules],
                "crawl_delay": group.crawl_delay,
            }
            for group in (rules.groups if rules else ())
        ],
        "content": robots.text,
    }


def _sitemap_section(report: AuditReport) -> dict[str, Any]:
    sitemaps = report.crawl.sitemaps
    urls = sitemaps.urls
    return {
        "checked": sitemaps.checked,
        "found": sitemaps.found,
        "complete": sitemaps.complete,
        "files": [_sitemap_file(f) for f in sitemaps.files],
        "url_count": len(urls),
        "urls": list(urls[:MAX_SITEMAP_URLS_IN_REPORT]),
        "urls_truncated": len(urls) > MAX_SITEMAP_URLS_IN_REPORT,
        "url_limit_reached": sitemaps.url_limit_reached,
        "file_limit_reached": sitemaps.file_limit_reached,
    }


def _sitemap_file(file: SitemapFile) -> dict[str, Any]:
    return {
        "url": file.url,
        "source": file.source,
        "guessed": file.guessed,
        "status_code": file.status_code,
        "valid": file.valid,
        "kind": file.kind,
        "looks_like_html": file.looks_like_html,
        "url_count": file.url_count,
        "error": file.error,
        "parse_error": file.parse_error,
        "invalid_urls": list(file.invalid_urls),
        "duplicate_urls": list(file.duplicate_urls),
        "cross_host_urls": list(file.cross_host_urls),
        "children": list(file.children),
    }
