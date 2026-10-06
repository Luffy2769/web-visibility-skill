"""robots.txt and XML sitemap findings.

robots.txt is evaluated **per crawler**: every token in
:data:`~web_visibility.robots.EVALUATED_CRAWLERS` gets its own verdict, using the
group that names it or, failing that, the ``*`` group. A wildcard block that
Googlebot is explicitly exempted from is therefore not reported as "all
crawlers blocked". Findings describe what the file *appears* to permit for a
token; they never predict what a search engine will index.

Sitemaps: only declared (robots.txt) and indexed sitemaps can produce defects.
``/sitemap.xml`` is merely a guess; an HTML page or 404 there is not a defect.
"""

from __future__ import annotations

from collections.abc import Iterable

from web_visibility.analyzers.base import AuditContext, joined_list, plural, sample
from web_visibility.models import Category, Issue, Page, Rule, Severity, SitemapFile
from web_visibility.robots import (
    AI_CRAWLERS,
    SEARCH_CRAWLERS,
    CrawlerProfile,
    RobotsTxt,
    RobotsVerdict,
)
from web_visibility.urls import normalize_url, origin

C = Category.TECHNICAL

ROBOTS_MISSING = Rule(
    "robots-txt-missing", C, Severity.LOW, "robots.txt not found",
    "Optional: add a robots.txt (it can be minimal) to state crawl rules and reference your "
    "sitemap. Without one, crawlers generally assume everything may be crawled.",
)  # fmt: skip
ROBOTS_DENIED = Rule(
    "robots-txt-access-denied", C, Severity.MEDIUM, "robots.txt access denied",
    "Serve robots.txt publicly with a 200 status; crawler behaviour for 401/403 varies.",
)  # fmt: skip
ROBOTS_RATE_LIMITED = Rule(
    "robots-txt-rate-limited", C, Severity.MEDIUM, "robots.txt request was rate limited",
    "Make sure robots.txt is not rate limited. Crawlers commonly treat a 429 here like a "
    "server error and postpone crawling.",
)  # fmt: skip
ROBOTS_UNREACHABLE = Rule(
    "robots-txt-unreachable", C, Severity.HIGH, "robots.txt unreachable",
    "Fix the server or network error. Under RFC 9309 crawlers should treat an unreachable "
    "robots.txt as a temporary complete disallow.",
)  # fmt: skip
ROBOTS_HTML = Rule(
    "robots-txt-not-plain-text", C, Severity.MEDIUM, "robots.txt appears to be an HTML page",
    "Serve a plain-text robots.txt (Content-Type: text/plain); an HTML page here usually means "
    "a catch-all route is answering instead of a real file.",
)  # fmt: skip
ROBOTS_DISALLOW_ALL = Rule(
    "robots-disallow-all", C, Severity.CRITICAL,
    "robots.txt appears to block every evaluated search crawler",
    "If the site should be discoverable in search, allow Googlebot and Bingbot (or remove "
    "\"Disallow: /\" from the groups that apply to them). Expected on staging sites.",
)  # fmt: skip
SEARCH_CRAWLER_BLOCKED = Rule(
    "robots-search-crawler-blocked", C, Severity.HIGH,
    "robots.txt appears to block a search crawler",
    "If the site should appear in this engine's results, allow its crawler in robots.txt.",
)  # fmt: skip
AI_CRAWLER_BLOCKED = Rule(
    "robots-ai-crawler-blocked", C, Severity.INFO, "robots.txt blocks AI crawlers",
    "No action needed if this is your policy. Blocking these tokens limits use of the site "
    "by the corresponding AI products; it does not affect classic search crawlers.",
)  # fmt: skip
START_DISALLOWED = Rule(
    "start-url-disallowed", C, Severity.HIGH, "Start URL appears disallowed for a search crawler",
    "Allow the start page in robots.txt for search crawlers if it should be crawled.",
)  # fmt: skip
CRAWLED_DISALLOWED = Rule(
    "crawled-page-disallowed", C, Severity.MEDIUM, "Pages appear disallowed for search crawlers",
    "Confirm these URLs are meant to be blocked; blocked pages cannot be crawled for content.",
)  # fmt: skip
LINKED_DISALLOWED = Rule(
    "linked-url-disallowed", C, Severity.INFO, "Internal links point to robots-disallowed URLs",
    "No action needed if intentional (e.g. account or cart pages).",
)  # fmt: skip
SITEMAP_DISALLOWED = Rule(
    "sitemap-url-disallowed", C, Severity.MEDIUM,
    "Sitemap lists URLs disallowed for search crawlers",
    "Remove blocked URLs from the sitemap, or unblock them; listing them sends mixed signals.",
)  # fmt: skip
SITEMAP_MISSING = Rule(
    "sitemap-missing", C, Severity.LOW, "No XML sitemap found",
    "Consider publishing an XML sitemap and referencing it from robots.txt with a "
    "\"Sitemap:\" line. Sitemaps are optional but help crawlers discover pages.",
)  # fmt: skip
SITEMAP_INACCESSIBLE = Rule(
    "sitemap-inaccessible", C, Severity.MEDIUM, "Declared sitemap could not be fetched",
    "Make sure every sitemap referenced in robots.txt or a sitemap index returns 200.",
)  # fmt: skip
SITEMAP_INVALID = Rule(
    "sitemap-invalid", C, Severity.MEDIUM, "Sitemap could not be parsed",
    "Serve well-formed XML using the sitemaps.org <urlset> or <sitemapindex> format.",
)  # fmt: skip
SITEMAP_EMPTY = Rule(
    "sitemap-empty", C, Severity.LOW, "Sitemap files list no URLs",
    "List the site's indexable URLs, or remove the empty sitemap references.",
)  # fmt: skip
SITEMAP_DUPLICATES = Rule(
    "sitemap-duplicate-urls", C, Severity.LOW, "Duplicate URLs in sitemap", "List each URL once.",
)  # fmt: skip
SITEMAP_BAD_URLS = Rule(
    "sitemap-invalid-urls", C, Severity.LOW, "Malformed URLs in sitemap",
    "Use absolute, fully-qualified http(s) URLs in every <loc> element.",
)  # fmt: skip
SITEMAP_CROSS_HOST = Rule(
    "sitemap-cross-host-urls", C, Severity.LOW, "Sitemap lists URLs on another host",
    "Per the sitemaps protocol, list only URLs on the sitemap's own host unless cross-submission "
    "has been set up.",
)  # fmt: skip
SITEMAP_INCOMPLETE = Rule(
    "sitemap-incomplete", C, Severity.INFO, "Sitemap list could not be read completely",
    "Sitemap-dependent checks were skipped or downgraded. Raise the limits or fix the failing "
    "sitemap files for a complete comparison.",
)  # fmt: skip
SITEMAP_URL_ERROR = Rule(
    "sitemap-url-error", C, Severity.MEDIUM, "Sitemap URL returns an error",
    "Remove URLs that return errors from the sitemap, or fix the pages.",
)  # fmt: skip
SITEMAP_URL_REDIRECT = Rule(
    "sitemap-url-redirects", C, Severity.LOW, "Sitemap lists redirecting URLs",
    "List final destination URLs in the sitemap instead of URLs that redirect.",
)  # fmt: skip
SITEMAP_NOINDEX = Rule(
    "sitemap-url-noindex", C, Severity.MEDIUM, "Sitemap lists noindex pages",
    "Sitemaps should list pages you want indexed: remove these from the sitemap, or remove "
    "the noindex if they should be indexed.",
)  # fmt: skip
SITEMAP_NON_CANONICAL = Rule(
    "sitemap-url-non-canonical", C, Severity.LOW, "Sitemap lists non-canonical URLs",
    "List the canonical URL of each page rather than its alternates.",
)  # fmt: skip
NOT_IN_SITEMAP = Rule(
    "page-not-in-sitemap", C, Severity.INFO, "Indexable pages missing from the sitemap",
    "If these pages should be indexed, consider adding them to the sitemap. (Only pages that "
    "are indexable, self-canonical and crawlable are listed here, so this never conflicts "
    "with the advice to remove noindex or non-canonical URLs.)",
)  # fmt: skip
SITEMAP_NOT_IN_ROBOTS = Rule(
    "sitemap-not-in-robots", C, Severity.INFO, "Sitemap not referenced in robots.txt",
    "Optional: add a \"Sitemap:\" line to robots.txt so crawlers can find it.",
)  # fmt: skip

RULES = (
    ROBOTS_MISSING, ROBOTS_DENIED, ROBOTS_RATE_LIMITED, ROBOTS_UNREACHABLE, ROBOTS_HTML,
    ROBOTS_DISALLOW_ALL, SEARCH_CRAWLER_BLOCKED, AI_CRAWLER_BLOCKED, START_DISALLOWED,
    CRAWLED_DISALLOWED, LINKED_DISALLOWED, SITEMAP_DISALLOWED, SITEMAP_MISSING,
    SITEMAP_INACCESSIBLE, SITEMAP_INVALID, SITEMAP_EMPTY, SITEMAP_DUPLICATES, SITEMAP_BAD_URLS,
    SITEMAP_CROSS_HOST, SITEMAP_INCOMPLETE, SITEMAP_URL_ERROR, SITEMAP_URL_REDIRECT,
    SITEMAP_NOINDEX, SITEMAP_NON_CANONICAL, NOT_IN_SITEMAP, SITEMAP_NOT_IN_ROBOTS,
)  # fmt: skip


def analyze(ctx: AuditContext) -> list[Issue]:
    return _robots_issues(ctx) + _sitemap_file_issues(ctx) + _sitemap_page_issues(ctx)


def blocked_search_crawlers(rules: RobotsTxt, url: str) -> list[RobotsVerdict]:
    """Verdicts for the search crawlers that may not fetch ``url``."""
    return [v for c in SEARCH_CRAWLERS if not (v := rules.verdict(url, c.token)).allowed]


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


def _robots_issues(ctx: AuditContext) -> list[Issue]:
    robots = ctx.crawl.robots
    detail = {"status": robots.status_code, "error": robots.error, "state": robots.state}
    if robots.state == "blocked":
        return []  # reported through the start URL; nothing was requested
    if robots.state in ("server-error", "unavailable"):
        transient = robots.state == "unavailable"
        what = robots.error or f"HTTP {robots.status_code}"
        return [ROBOTS_UNREACHABLE.issue(
            description="robots.txt returned a server error or could not be reached"
            + (" (a network failure that may be temporary)." if transient else "."),
            evidence=f"{robots.url}: {what}",
            confidence=0.6 if transient else 0.85, details=detail,
        )]  # fmt: skip
    if robots.state == "rate-limited":
        return [ROBOTS_RATE_LIMITED.issue(
            description="robots.txt answered HTTP 429, so the auditor did not crawl the site.",
            evidence=f"{robots.url}: HTTP 429", confidence=0.7, details=detail,
        )]  # fmt: skip
    if robots.state == "inaccessible":
        return [ROBOTS_DENIED.issue(
            description=f"robots.txt returned HTTP {robots.status_code}.",
            evidence=f"{robots.url}: HTTP {robots.status_code}", confidence=0.9, details=detail,
        )]  # fmt: skip
    if robots.state == "missing" or robots.rules is None:
        return [ROBOTS_MISSING.issue(
            description="No robots.txt file was found at the site root.",
            evidence=f"{robots.url}: HTTP {robots.status_code}", confidence=0.95, details=detail,
        )]  # fmt: skip

    issues: list[Issue] = []
    rules = robots.rules
    if robots.looks_like_html:
        issues.append(ROBOTS_HTML.issue(
            description="The robots.txt response looks like an HTML document.",
            evidence=f"Content-Type: {robots.content_type or 'missing'}; body begins "
                     f"{(robots.text or '')[:60].strip()!r}",
            confidence=0.85,
        ))  # fmt: skip
    issues += _crawler_access_issues(ctx, rules)

    blocked_pages = {
        p.final_url: [v.agent for v in blocked_search_crawlers(rules, p.final_url)]
        for p in ctx.pages
        if not ctx.is_start(p)
    }
    blocked_pages = {url: agents for url, agents in blocked_pages.items() if agents}
    if blocked_pages:
        urls = list(blocked_pages)
        issues.append(CRAWLED_DISALLOWED.issue(
            description=f"{plural(len(urls), 'crawled page')} appear disallowed for search "
                        "crawlers by the site's robots.txt rules.",
            evidence="; ".join(f"{u} ({', '.join(a)})" for u, a in list(blocked_pages.items())[:5]),
            confidence=0.85, affected_urls=urls, details={"agents_by_url": blocked_pages},
        ))  # fmt: skip

    own = (ctx.start_url, ctx.site_url)
    skipped = [
        u
        for u, reason in ctx.crawl.skipped.items()
        if reason == "robots-disallowed" and u not in own
    ]
    if skipped:
        issues.append(LINKED_DISALLOWED.issue(
            description=f"{plural(len(skipped), 'linked URL')} were not crawled because "
                        "robots.txt appears to disallow them for this auditor.",
            evidence=f"Skipped: {joined_list(skipped)}",
            confidence=0.85, details={"urls": sample(skipped, 50)},
        ))  # fmt: skip

    sitemap_blocked = [u for u in ctx.crawl.sitemaps.urls if blocked_search_crawlers(rules, u)]
    if sitemap_blocked:
        issues.append(SITEMAP_DISALLOWED.issue(
            description=f"{plural(len(sitemap_blocked), 'sitemap URL')} appear disallowed for "
                        "search crawlers by robots.txt.",
            evidence=f"Listed in sitemap but disallowed: {joined_list(sitemap_blocked)}",
            confidence=0.85, details={"urls": sample(sitemap_blocked, 50)},
            prevalence=len(sitemap_blocked) / max(1, len(ctx.crawl.sitemaps.urls)),
        ))  # fmt: skip
    return issues


def _crawler_access_issues(ctx: AuditContext, rules: RobotsTxt) -> list[Issue]:
    """Root-level access per crawler, then the start URL for search crawlers."""
    issues: list[Issue] = []
    root_blocked = {c.token: rules.verdict("/", c.token) for c in SEARCH_CRAWLERS}
    root_blocked = {t: v for t, v in root_blocked.items() if rules.disallows_everything(t)}

    if root_blocked and len(root_blocked) == len(SEARCH_CRAWLERS):
        issues.append(ROBOTS_DISALLOW_ALL.issue(
            description="Every evaluated search crawler appears disallowed from the whole site.",
            evidence="; ".join(f"{t}: {v.explanation}" for t, v in root_blocked.items()),
            confidence=0.9, details={"crawlers": _verdict_details(root_blocked.values())},
        ))  # fmt: skip
    else:
        for token, verdict in root_blocked.items():
            issues.append(SEARCH_CRAWLER_BLOCKED.issue(
                description=f"{token} appears disallowed from the whole site by robots.txt.",
                evidence=f"{token}: {verdict.explanation}",
                confidence=0.9, details={"crawler": token, **_verdict_details([verdict])[0]},
            ))  # fmt: skip

    ai_blocked = [c for c in AI_CRAWLERS if rules.disallows_everything(c.token)]
    if ai_blocked:
        verdicts = [rules.verdict("/", c.token) for c in ai_blocked]
        issues.append(AI_CRAWLER_BLOCKED.issue(
            description=f"{plural(len(ai_blocked), 'AI crawler token')} appear disallowed from "
                        f"the whole site: {', '.join(c.token for c in ai_blocked)}.",
            evidence="; ".join(f"{v.agent}: {v.explanation}" for v in verdicts),
            confidence=0.9, details={"crawlers": _verdict_details(verdicts)},
        ))  # fmt: skip

    start = ctx.site_url
    start_blocked = [
        v for v in blocked_search_crawlers(rules, start) if v.agent not in root_blocked
    ]
    if start_blocked:
        issues.append(START_DISALLOWED.issue(
            description="The start URL appears disallowed by robots.txt for "
                        f"{', '.join(v.agent for v in start_blocked)}.",
            evidence="; ".join(f"{v.agent}: {v.explanation}" for v in start_blocked),
            confidence=0.85, affected_urls=(start,),
            details={"crawlers": _verdict_details(start_blocked)},
        ))  # fmt: skip
    return issues


def crawler_access_summary(ctx: AuditContext) -> list[dict[str, object]]:
    """Per-crawler access to the site root and start URL (for reports)."""
    robots = ctx.crawl.robots
    rows: list[dict[str, object]] = []
    profiles: tuple[CrawlerProfile, ...] = (*SEARCH_CRAWLERS, *AI_CRAWLERS)
    for profile in profiles:
        root = robots.verdict(origin(ctx.site_url) + "/", profile.token)
        start = robots.verdict(ctx.site_url, profile.token)
        rows.append({
            "crawler": profile.token,
            "purpose": profile.purpose,
            "operator": profile.operator,
            "root_allowed": root.allowed,
            "start_url_allowed": start.allowed,
            "group": start.group,
            "rule": str(start.rule) if start.rule else None,
            "explicit_group": bool(robots.rules and robots.rules.names_agent(profile.token)),
        })  # fmt: skip
    return rows


def _verdict_details(verdicts: Iterable[RobotsVerdict]) -> list[dict[str, object]]:
    return [
        {"crawler": v.agent, "group": v.group, "rule": str(v.rule) if v.rule else None}
        for v in verdicts
    ]


# --------------------------------------------------------------------------- #
# Sitemap documents
# --------------------------------------------------------------------------- #


def _sitemap_file_issues(ctx: AuditContext) -> list[Issue]:
    sitemaps = ctx.crawl.sitemaps
    if not sitemaps.checked:
        return []  # nothing was requested (e.g. host unreachable): make no claim
    issues: list[Issue] = []
    empty: list[SitemapFile] = []
    for file in sitemaps.files:
        if file.guessed and not _guess_is_evidence(file):
            continue  # /sitemap.xml is only a guess: a 404 or HTML page there is not a defect
        if file.valid and file.url_count == 0 and not file.invalid_urls:
            empty.append(file)
            continue
        issues += _file_issues(file)

    if empty:
        issues.append(SITEMAP_EMPTY.issue(
            description=f"{plural(len(empty), 'sitemap file')} contained no URLs.",
            evidence=f"0 <loc> entries in: {joined_list(f.url for f in empty)}",
            confidence=0.95, details={"files": [f.url for f in empty]},
            prevalence=len(empty) / max(1, len(sitemaps.files)),
        ))  # fmt: skip

    if not sitemaps.found:
        issues.append(SITEMAP_MISSING.issue(
            description="No valid sitemap was found in robots.txt or at /sitemap.xml.",
            evidence="Sitemap locations tried: "
                     + "; ".join(f"{f.url} ({_file_state(f)})" for f in sitemaps.files) + ".",
            confidence=0.8,
        ))  # fmt: skip
    elif ctx.crawl.robots.exists and not any(f.source == "robots" for f in sitemaps.files):
        issues.append(SITEMAP_NOT_IN_ROBOTS.issue(
            description="A sitemap exists at /sitemap.xml but robots.txt does not reference it.",
            evidence=f"robots.txt at {ctx.crawl.robots.url} has no Sitemap: directive.",
            confidence=0.95,
        ))  # fmt: skip

    if sitemaps.found and not sitemaps.complete:
        reasons = []
        if sitemaps.file_limit_reached:
            reasons.append(f"stopped after {ctx.crawl.config.max_sitemaps} sitemap files")
        if sitemaps.url_limit_reached:
            reasons.append(f"stopped after {ctx.crawl.config.max_sitemap_urls} URLs")
        failed = [f.url for f in sitemaps.files if not f.guessed and not f.valid]
        if failed:
            reasons.append(f"{len(failed)} declared sitemap file(s) could not be read")
        issues.append(SITEMAP_INCOMPLETE.issue(
            description="Only part of the sitemap list was read, so checks that need the "
                        "complete list (page-not-in-sitemap) were skipped.",
            evidence="; ".join(reasons) or "incomplete",
            confidence=1.0, details={"reasons": reasons},
        ))  # fmt: skip
    return issues


def _guess_is_evidence(file: SitemapFile) -> bool:
    """A guessed /sitemap.xml only counts if it is clearly meant to be a sitemap:
    it parsed, or it is non-HTML XML that failed to parse."""
    if file.valid:
        return True
    return file.fetched and file.parse_error is not None and not file.looks_like_html


def _file_state(file: SitemapFile) -> str:
    if file.looks_like_html:
        return "an HTML page, not a sitemap"
    if file.error:
        return file.error_kind or "error"
    if not file.fetched:
        return f"HTTP {file.status_code}"
    return file.parse_error or "valid"


def _file_issues(file: SitemapFile) -> list[Issue]:
    details = {"sitemap": file.url, "source": file.source}
    if not file.fetched:
        evidence = file.error or f"HTTP {file.status_code}"
        return [SITEMAP_INACCESSIBLE.issue(
            description=f"The sitemap at {file.url} ({_source_label(file)}) could not be "
                        "fetched.",
            evidence=f"{file.url}: {evidence}",
            confidence=0.9, details={**details, "status": file.status_code},
        )]  # fmt: skip
    if file.parse_error:
        return [SITEMAP_INVALID.issue(
            description=f"The sitemap at {file.url} ({_source_label(file)}) is not a valid "
                        "sitemap document.",
            evidence=f"{file.url}: {file.parse_error}",
            confidence=0.8 if file.guessed else 0.95, details=details,
        )]  # fmt: skip

    issues: list[Issue] = []
    entries = max(1, file.url_count + len(file.invalid_urls))
    if file.duplicate_urls:
        issues.append(SITEMAP_DUPLICATES.issue(
            description=f"{plural(len(file.duplicate_urls), 'URL')} appear more than once.",
            evidence=f"{file.url} repeats: {joined_list(file.duplicate_urls)}",
            confidence=0.97, details={**details, "urls": list(file.duplicate_urls)},
            prevalence=len(file.duplicate_urls) / entries,
        ))  # fmt: skip
    if file.invalid_urls:
        issues.append(SITEMAP_BAD_URLS.issue(
            description=f"{plural(len(file.invalid_urls), '<loc> value')} are not absolute "
                        "http(s) URLs.",
            evidence=f"{file.url}: {joined_list(file.invalid_urls, quote=True)}",
            confidence=0.95, details={**details, "urls": sample(file.invalid_urls, 50)},
            prevalence=len(file.invalid_urls) / entries,
        ))  # fmt: skip
    if file.cross_host_urls:
        issues.append(SITEMAP_CROSS_HOST.issue(
            description=f"{plural(len(file.cross_host_urls), 'URL')} are on a different host "
                        "than the sitemap.",
            evidence=f"{file.url}: {joined_list(file.cross_host_urls)}",
            confidence=0.8, details={**details, "urls": sample(file.cross_host_urls, 50)},
            prevalence=len(file.cross_host_urls) / entries,
        ))  # fmt: skip
    return issues


def _source_label(file: SitemapFile) -> str:
    return {
        "robots": "referenced in robots.txt",
        "index": "listed in a sitemap index",
        "default": "guessed default location",
    }[file.source]


# --------------------------------------------------------------------------- #
# Sitemap vs crawl comparison
# --------------------------------------------------------------------------- #


def _sitemap_page_issues(ctx: AuditContext) -> list[Issue]:
    sitemaps = ctx.crawl.sitemaps
    sitemap_urls = set(sitemaps.urls)
    if not sitemap_urls:
        return []
    errors: list[str] = []
    redirects: list[str] = []
    noindex: list[str] = []
    non_canonical: list[str] = []
    for url in sitemaps.urls:
        page = ctx.page_index.get(url)
        if page is None:
            continue
        if page.error or not page.is_success:
            errors.append(f"{url} ({page.error or f'HTTP {page.status_code}'})")
        elif page.redirect_chain and page.requested_url == url:
            redirects.append(f"{url} -> {page.final_url}")
        if page.content is None:
            continue
        if page.noindex_for_any_agent:
            who = "all crawlers" if page.is_noindex else ", ".join(page.noindex_agents)
            noindex.append(f"{url} (noindex for {who})")
        canonical = normalize_url(page.canonical, page.final_url) if page.canonical else None
        if canonical and canonical != page.final_url:
            non_canonical.append(f"{url} (canonical: {canonical})")

    issues: list[Issue] = []
    for rule, found, what in (
        (SITEMAP_URL_ERROR, errors, "return an error"),
        (SITEMAP_URL_REDIRECT, redirects, "redirect"),
        (SITEMAP_NOINDEX, noindex, "are marked noindex"),
        (SITEMAP_NON_CANONICAL, non_canonical, "declare a different canonical URL"),
    ):
        if found:
            urls = [entry.split(" ", 1)[0] for entry in found]
            issues.append(rule.issue(
                description=f"{plural(len(found), 'sitemap URL')} {what}.",
                evidence=joined_list(found),
                confidence=0.9, affected_urls=urls, details={"entries": found},
            ))  # fmt: skip

    if not sitemaps.complete:
        return issues  # the list is partial: "not in sitemap" would be guesswork
    rules = ctx.crawl.robots.rules
    missing = [
        p.final_url
        for p in ctx.pages
        if not p.noindex_for_any_agent
        and p.final_url not in sitemap_urls
        and p.requested_url not in sitemap_urls
        and _self_canonical(p)
        and not (rules and blocked_search_crawlers(rules, p.final_url))
    ]
    if missing:
        issues.append(NOT_IN_SITEMAP.issue(
            description=f"{plural(len(missing), 'indexable, self-canonical page')} are not "
                        "listed in any sitemap.",
            evidence=f"Not in sitemap: {joined_list(missing)}",
            confidence=0.9, affected_urls=missing,
        ))  # fmt: skip
    return issues


def _self_canonical(page: Page) -> bool:
    if not page.canonical:
        return True
    return normalize_url(page.canonical, page.final_url) == page.final_url
