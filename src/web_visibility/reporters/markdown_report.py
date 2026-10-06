"""Human-readable Markdown report (``audit.md``).

All text originating from the audited site is escaped with :func:`md` /
:func:`md_code` so a hostile page cannot inject Markdown or HTML.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from web_visibility.analyzers.crawlability import crawler_access_summary
from web_visibility.audit import AuditReport
from web_visibility.models import Issue, Severity
from web_visibility.reporters._text import md, md_code
from web_visibility.reporters.json_report import LIMITATIONS
from web_visibility.scoring import SCORE_DISCLAIMER, SCORE_NAME

MAX_AFFECTED_LISTED = 10


def render_markdown(report: AuditReport) -> str:
    sections = [
        _header(report),
        _executive_summary(report),
        _score(report),
        _crawl_summary(report),
        _issues(report),
        _recommendations(report),
        _technical_findings(report),
        _pages(report),
        _limitations(),
    ]
    return "\n\n".join(s.rstrip() for s in sections if s) + "\n"


def write_markdown(report: AuditReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #


def _header(report: AuditReport) -> str:
    return "\n".join(
        [
            "# Web Visibility Audit",
            "",
            f"- **Target:** {md_code(report.target_url)}",
            f"- **Generated:** {report.generated_at}",
            f"- **Tool version:** web-visibility {report.version} (Phase 01: technical foundation)",
        ]
    )


def _executive_summary(report: AuditReport) -> str:
    counts = Counter(i.severity for i in report.issues)
    lines = ["## Executive Summary", ""]
    crawl = report.crawl
    if not report.auditable:
        lines.append(
            "**Audit incomplete:** the start URL could not be audited, so no score was "
            "computed. This is not evidence that the site is empty or broken; see the "
            "issues below for the reason."
        )
    else:
        partial = " (partial coverage)" if report.score.status == "partial" else ""
        lines.append(
            f"Audited **{report.pages_audited}** page(s) of "
            f"{md_code(crawl.start_url)}. **{SCORE_NAME}: "
            f"{report.score.overall}/100{partial}.**"
        )
    reasons = f" ({md('; '.join(crawl.state_reasons))})" if crawl.state_reasons else ""
    lines += ["", f"Audit state: **{crawl.state}**{reasons}"]
    severity_text = (
        ", ".join(f"{counts[s]} {s.value}" for s in Severity if counts[s]) or "no issues"
    )
    lines += ["", f"Findings: {len(report.issues)} ({severity_text})."]

    top = [i for i in report.issues if i.severity != Severity.INFO][:5]
    if top:
        lines += ["", "**Top issues:**", ""]
        lines += [
            f"{n}. **{md(i.title)}** ({i.severity.value}): {md(i.description, 200)}"
            for n, i in enumerate(top, 1)
        ]
    if crawl.limit_reached:
        lines += [
            "",
            f"> Note: {len(crawl.pending)} discovered URL(s) were not crawled. Findings "
            "describe the crawled subset only.",
        ]
    return "\n".join(lines)


def _score(report: AuditReport) -> str:
    if not report.auditable:
        return ""
    lines = [
        "## Score",
        "",
        f"**{SCORE_NAME}: {report.score.overall}/100**",
        "",
        f"> {SCORE_DISCLAIMER}",
        "",
        "### Category Scores",
        "",
        "Categories marked N/A could not be checked and are excluded from the overall "
        "score; they are never counted as full marks.",
        "",
        "| Category | Score | Max | Coverage | Largest deductions / note |",
        "|---|---:|---:|---|---|",
    ]
    for c in report.score.categories:
        cov = c.coverage
        coverage = f"{cov.status}: {cov.checked}/{cov.applicable} {cov.unit}"
        if c.points is None:
            reason = md(cov.reason or "")
            lines.append(f"| {c.category.label} | N/A | {c.max_points} | {coverage} | {reason} |")
            continue
        top = ", ".join(f"{d.rule_id} (-{d.points:g})" for d in c.deductions[:3]) or "none"
        if cov.status == "partial" and cov.reason:
            top += f"; partial: {md(cov.reason)}"
        lines.append(f"| {c.category.label} | {c.points:g} | {c.max_points} | {coverage} | {top} |")
    return "\n".join(lines)


def _crawl_summary(report: AuditReport) -> str:
    crawl = report.crawl
    config = crawl.config
    skipped = Counter(crawl.skipped.values())
    rows = [
        ("Start URL", md_code(crawl.start_url)),
        ("Site crawled", md_code(crawl.site_url)),
        ("Pages crawled", str(len(crawl.pages))),
        ("Pages audited (2xx HTML)", str(report.pages_audited)),
        ("Page limit", f"{config.max_pages}" + (" (reached)" if crawl.limit_reached else "")),
        ("Discovered but not crawled", str(len(crawl.pending))),
        ("Link targets checked", str(len(crawl.link_checks))),
        ("Skipped URLs", ", ".join(f"{n} {r}" for r, n in skipped.items()) or "0"),
        ("robots.txt respected", "yes" if config.respect_robots else "no (--ignore-robots)"),
        ("External links checked", "yes" if config.check_external else "no"),
        ("HTTP requests", str(crawl.request_count)),
        ("Duration", f"{crawl.duration_seconds:.1f}s"),
    ]
    lines = ["## Crawl Summary", "", "| | |", "|---|---|"]
    lines += [f"| {label} | {value} |" for label, value in rows]
    return "\n".join(lines)


def _issues(report: AuditReport) -> str:
    lines = ["## Issues", ""]
    if not report.issues:
        return "\n".join([*lines, "No issues were detected by the Phase 01 checks."])
    for severity in Severity:
        group = [i for i in report.issues if i.severity == severity]
        if not group:
            continue
        lines += [f"### {severity.value.capitalize()} ({len(group)})", ""]
        for issue in group:
            lines += _issue_block(issue)
    return "\n".join(lines)


def _issue_block(issue: Issue) -> list[str]:
    lines = [
        f"#### {md(issue.title)} {md_code(issue.id)}",
        "",
        f"- **Category:** {issue.category.label}",
        f"- **Confidence:** {issue.confidence:.0%}",
        f"- **What:** {md(issue.description)}",
        f"- **Evidence:** {md(issue.evidence, 600)}",
        f"- **Recommendation:** {md(issue.recommendation)}",
    ]
    if issue.affected_urls:
        lines.append(f"- **Affected pages ({len(issue.affected_urls)}):**")
        lines += [f"  - {md_code(url, 200)}" for url in issue.affected_urls[:MAX_AFFECTED_LISTED]]
        if len(issue.affected_urls) > MAX_AFFECTED_LISTED:
            lines.append(f"  - ... and {len(issue.affected_urls) - MAX_AFFECTED_LISTED} more")
    else:
        lines.append("- **Scope:** site-wide")
    lines.append("")
    return lines


def _recommendations(report: AuditReport) -> str:
    """Unique recommendations, ordered by the most severe issue that triggered them."""
    seen: dict[str, Issue] = {}
    for issue in report.issues:
        if issue.severity != Severity.INFO and issue.id not in seen:
            seen[issue.id] = issue
    if not seen:
        return ""
    lines = ["## Recommendations", "", "Ordered by severity, then by number of affected pages.", ""]
    for n, issue in enumerate(seen.values(), 1):
        count = sum(1 for i in report.issues if i.id == issue.id)
        lines.append(
            f"{n}. **{md(issue.title)}** ({issue.severity.value}, {count} finding(s)): "
            f"{md(issue.recommendation)}"
        )
    return "\n".join(lines)


def _technical_findings(report: AuditReport) -> str:
    robots = report.crawl.robots
    sitemaps = report.crawl.sitemaps
    lines = ["## Technical Findings", "", "### robots.txt", ""]
    if robots.exists and robots.rules is not None:
        groups = len(robots.rules.groups)
        lines.append(
            f"- Found at {md_code(robots.url)} (HTTP {robots.status_code}), "
            f"{groups} user-agent group(s)."
        )
        lines += [f"- Sitemap reference: {md_code(s)}" for s in robots.rules.sitemaps]
        lines += ["", "| Crawler | Type | Site root | Deciding rule |", "|---|---|---|---|"]
        for row in crawler_access_summary(report.context):
            group, rule = row["group"], row["rule"] or "no matching rule"
            where = f"User-agent: {group} -> {rule}" if group else "no group applies"
            access = "allowed" if row["root_allowed"] else "**BLOCKED**"
            lines.append(f"| {row['crawler']} | {row['purpose']} | {access} | {md(where)} |")
    else:
        status = robots.error or f"HTTP {robots.status_code}"
        lines.append(f"- Not available at {md_code(robots.url)} ({md(status)}).")

    lines += ["", "### Sitemaps", ""]
    if not sitemaps.files:
        lines.append("- No sitemap locations were checked.")
    for file in sitemaps.files:
        if file.looks_like_html:
            state = "HTML page, not a sitemap"
            if file.guessed:
                state += " (guessed location; not treated as a defect)"
        elif file.valid:
            state = f"{file.kind}, {file.url_count} URL(s)"
        elif file.parse_error:
            state = f"invalid: {md(file.parse_error)}"
        else:
            state = md(file.error or f"HTTP {file.status_code}")
        lines.append(f"- {md_code(file.url)} ({file.source}): {state}")
    incomplete = "" if sitemaps.complete else " (incomplete: limits reached or files failed)"
    lines.append(f"- Unique URLs listed across sitemaps: {len(sitemaps.urls)}{incomplete}")

    if report.structured_data_types:
        lines += ["", "### Structured data types (JSON-LD)", ""]
        lines += [f"- {md_code(t)}: {n} page(s)" for t, n in report.structured_data_types.items()]
    return "\n".join(lines)


def _pages(report: AuditReport) -> str:
    audited = {p.final_url for p in report.context.pages}
    lines = [
        "## Affected Pages",
        "",
        "| URL | Status | Title | H1s | Issues |",
        "|---|---:|---|---:|---:|",
    ]
    counts: Counter[str] = Counter()
    for issue in report.issues:
        if issue.severity != Severity.INFO:
            counts.update(set(issue.affected_urls))
    for page in report.crawl.pages:
        status = (
            str(page.status_code)
            if page.status_code is not None
            else md(page.error_kind or "error")
        )
        title = md(page.title, 60) if page.title else ("-" if page.final_url in audited else "n/a")
        issue_count = counts[page.final_url]
        if page.requested_url != page.final_url:
            issue_count += counts[page.requested_url]
        lines.append(
            f"| {md_code(page.final_url, 90)} | {status} | {title} | "
            f"{len(page.h1s)} | {issue_count} |"
        )
    return "\n".join(lines)


def _limitations() -> str:
    return "\n".join(["## Limitations", "", *[f"- {item}" for item in LIMITATIONS]])
