"""Human-friendly terminal report using Rich.

Text from the audited site is always wrapped in :class:`rich.text.Text` (never
interpolated into markup strings) and passed through :func:`clean`, so page
titles or URLs cannot inject console markup or escape sequences.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from web_visibility.analyzers.crawlability import crawler_access_summary
from web_visibility.audit import AuditReport
from web_visibility.models import Issue, Severity
from web_visibility.reporters._text import clean
from web_visibility.scoring import SCORE_NAME

SEVERITY_STYLES = {
    Severity.CRITICAL: "bold white on red",
    Severity.HIGH: "bold red",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
    Severity.INFO: "dim",
}
SEVERITY_LABELS = {
    Severity.CRITICAL: "CRIT",
    Severity.HIGH: "HIGH",
    Severity.MEDIUM: "MED",
    Severity.LOW: "LOW",
    Severity.INFO: "INFO",
}


def render_terminal(
    report: AuditReport,
    console: Console,
    *,
    max_issues: int = 25,
    show_info: bool = False,
    written: list[Path] | None = None,
) -> None:
    console.print()
    console.rule(Text("WEB VISIBILITY AUDIT", style="bold"))
    console.print(_summary(report))
    if report.auditable:
        console.print(_categories(report, unicode=console.encoding.lower().startswith("utf")))
        robots = _robots(report)
        if robots is not None:
            console.print(robots)
    console.print(_issues(report, max_issues=max_issues, show_info=show_info))
    if written:
        console.print(Text("Reports written:", style="bold"))
        for path in written:
            console.print(Text(f"  {path}"))
    console.print(
        Text(
            "Diagnostic score only - not a ranking, traffic or AI-visibility prediction.",
            style="dim italic",
        )
    )


def _summary(report: AuditReport) -> Panel:
    crawl = report.crawl
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("URL", Text(clean(crawl.start_url, 120)))
    if crawl.site_url != crawl.start_url:
        grid.add_row("Crawled as", Text(clean(crawl.site_url, 120)))
    pages = f"{len(crawl.pages)} crawled, {report.pages_audited} audited"
    if crawl.limit_reached:
        pages += f" (limit {crawl.config.max_pages} reached, {len(crawl.pending)} not crawled)"
    grid.add_row("Pages", pages)
    grid.add_row("Requests", f"{crawl.request_count} in {crawl.duration_seconds:.1f}s")
    state_style = {"complete": "green", "partial": "yellow", "failed": "bold red"}[crawl.state]
    audit = Text(crawl.state.upper(), style=state_style)
    if crawl.state_reasons:
        audit += Text(" - " + clean("; ".join(crawl.state_reasons), 160), style="dim")
    grid.add_row("Audit", audit)
    score = report.score
    if score.overall is not None:
        label = Text(f"{score.overall}/100", style=_score_style(score.overall))
        if score.status == "partial":
            label += Text("  PARTIAL", style="bold yellow")
            label += Text(f" ({clean(score.reason or '', 100)})", style="dim")
        grid.add_row("Score", label)
    else:
        why = clean(score.reason or "the audit is incomplete", 140)
        grid.add_row("Score", Text(f"not scored - {why}", style="bold red"))
    return Panel(grid, title=SCORE_NAME, title_align="left", border_style="blue")


def _categories(report: AuditReport, *, unicode: bool = True) -> Table:
    full, empty = ("█", "░") if unicode else ("#", "-")
    table = Table(show_edge=False, header_style="bold", pad_edge=False)
    table.add_column("Category")
    table.add_column("Score", justify="right")
    table.add_column("", min_width=20)
    table.add_column("Coverage", justify="right")
    table.add_column("Top deduction / note", style="dim")
    for c in report.score.categories:
        cov = c.coverage
        coverage = f"{cov.checked}/{cov.applicable} {cov.unit}" if cov.applicable else "-"
        if c.points is None:
            table.add_row(c.category.label, "N/A", Text("not scored", style="dim"), coverage,
                          clean(cov.reason or "", 60))  # fmt: skip
            continue
        ratio = c.points / c.max_points if c.max_points else 1.0
        filled = round(ratio * 20)
        bar = Text(full * filled, style=_score_style(round(ratio * 100)))
        bar += Text(empty * (20 - filled), style="dim")
        note = c.deductions[0].rule_id if c.deductions else ""
        if cov.status == "partial":
            note = f"partial: {cov.reason}" if not note else f"{note} (partial)"
        table.add_row(c.category.label, f"{c.points:g}/{c.max_points}", bar, coverage,
                      clean(note, 60))  # fmt: skip
    return table


def _robots(report: AuditReport) -> Table | None:
    robots = report.crawl.robots
    if robots.rules is None:
        return None
    table = Table(title="ROBOTS.TXT (per crawler)", title_justify="left", show_edge=False,
                  header_style="bold", pad_edge=False)  # fmt: skip
    table.add_column("Crawler")
    table.add_column("Type")
    table.add_column("Site root", justify="center")
    table.add_column("Deciding rule", style="dim")
    for row in crawler_access_summary(report.context):
        allowed = bool(row["root_allowed"])
        verdict = Text(
            "allowed" if allowed else "BLOCKED", style="green" if allowed else "bold red"
        )
        group = row["group"]
        rule = row["rule"] or ("no matching rule" if group else "no group applies")
        where = f"User-agent: {group} -> {rule}" if group else str(rule)
        table.add_row(str(row["crawler"]), str(row["purpose"]), verdict, clean(where, 60))
    return table


def _issues(report: AuditReport, *, max_issues: int, show_info: bool) -> Group:
    counts = Counter(i.severity for i in report.issues)
    header = Text("\nISSUES  ", style="bold")
    for severity in Severity:
        if counts[severity]:
            header += Text(
                f" {counts[severity]} {severity.value} ", style=SEVERITY_STYLES[severity]
            )
            header += Text(" ")
    if not report.issues:
        header += Text("none detected by the Phase 01 checks", style="green")

    shown = [i for i in report.issues if show_info or i.severity != Severity.INFO]
    rows = [_issue_line(issue) for issue in shown[:max_issues]]
    hidden = len(shown) - len(rows)
    footer = []
    if hidden > 0:
        footer.append(
            Text(f"  ... {hidden} more issue(s) in the JSON/Markdown report", style="dim")
        )
    if not show_info and counts[Severity.INFO]:
        footer.append(
            Text(
                f"  {counts[Severity.INFO]} informational note(s) hidden (use --show-info)",
                style="dim",
            )
        )
    return Group(header, *rows, *footer, Text(""))


def _issue_line(issue: Issue) -> Text:
    label = Text(
        f"[{SEVERITY_LABELS[issue.severity]}]".ljust(7), style=SEVERITY_STYLES[issue.severity]
    )
    line = Text("  ") + label + Text(clean(issue.title), style="bold")
    scope = f"{len(issue.affected_urls)} page(s)" if issue.affected_urls else "site-wide"
    line += Text(f"  {scope}", style="dim")
    line += Text("\n          ") + Text(clean(issue.evidence, 160), style="dim")
    return line


def _score_style(value: int) -> str:
    if value >= 80:
        return "bold green"
    if value >= 50:
        return "bold yellow"
    return "bold red"
