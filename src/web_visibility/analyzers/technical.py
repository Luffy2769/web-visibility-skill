"""HTTP status, HTTPS, redirects, rendering, indexability and crawl coverage.

robots.txt and sitemap checks live in :mod:`web_visibility.analyzers.crawlability`.
"""

from __future__ import annotations

from web_visibility.analyzers.base import AuditContext, joined_list, plural, sample
from web_visibility.models import UNRETRIEVED_STATUSES, Category, Issue, Page, Rule, Severity
from web_visibility.safety import is_local_host
from web_visibility.urls import host_of, same_origin

C = Category.TECHNICAL

#: Fetch failures that may well succeed on a retry; findings about them are hedged.
TRANSIENT_ERROR_KINDS = frozenset({"timeout", "connection", "dns"})

START_URL_FAILED = Rule(
    "start-url-unavailable", C, Severity.CRITICAL, "Start URL could not be audited",
    "Make sure the URL is publicly reachable and returns an HTML page with a 2xx status, "
    "then re-run the audit.",
)  # fmt: skip
HTTPS_NOT_USED = Rule(
    "https-not-used", C, Severity.HIGH, "Site is served over plain HTTP",
    "Serve the site over HTTPS and redirect http:// requests to https://.",
)  # fmt: skip
MIXED_CONTENT = Rule(
    "mixed-content", C, Severity.MEDIUM, "Mixed content on an HTTPS page",
    "Load every image, script, stylesheet and frame over https://; browsers may block or "
    "warn about insecure subresources.",
)  # fmt: skip
REDIRECT_CHAIN = Rule(
    "redirect-chain", C, Severity.LOW, "Redirect chain",
    "Redirect directly to the final URL in a single hop.",
)  # fmt: skip
PAGE_FETCH_ERROR = Rule(
    "page-fetch-error", C, Severity.MEDIUM, "Page could not be fetched",
    "Check the URL in a browser and the server logs; the auditor received no usable response.",
)  # fmt: skip
CRAWLED_PAGE_ERROR = Rule(
    "crawled-page-error", C, Severity.HIGH, "Linked pages return HTTP errors",
    "Restore these pages, redirect them to a working equivalent, or remove the links to them.",
)  # fmt: skip
CLIENT_RENDERED = Rule(
    "client-rendered-shell", C, Severity.MEDIUM, "Page appears to be a client-rendered shell",
    "The server response contains an application shell with little server-rendered content; "
    "browser rendering may be required for a complete audit. If search and AI visibility "
    "matter, consider server-side rendering or pre-rendering the important content.",
)  # fmt: skip
NOINDEX_START = Rule(
    "noindex-start-page", C, Severity.HIGH, "Start page is marked noindex",
    "Remove the noindex directive if the page should be eligible for search results.",
)  # fmt: skip
NOINDEX_PAGE = Rule(
    "noindex-page", C, Severity.INFO, "Page is marked noindex",
    "No action needed if intentional; noindex asks crawlers not to index the page.",
)  # fmt: skip
NOFOLLOW_PAGE = Rule(
    "nofollow-page", C, Severity.INFO, "Page-level nofollow directive",
    "No action needed if intentional; nofollow asks crawlers not to follow any link on the page.",
)  # fmt: skip
PAGE_TOO_LARGE = Rule(
    "page-too-large", C, Severity.LOW, "Page exceeded the size limit and was not analyzed",
    "Very large HTML documents are slow for visitors. Re-run with a larger --max-page-bytes "
    "if this page must be analyzed.",
)  # fmt: skip
CRAWL_LIMIT = Rule(
    "crawl-limit-reached", C, Severity.INFO, "Crawl stopped at the page limit",
    "Re-run with a higher --max-pages for a more complete audit.",
)  # fmt: skip
CRAWL_STOPPED = Rule(
    "crawl-stopped-early", C, Severity.INFO, "Crawl stopped early to respect the server",
    "Re-run later, or with the site owner's agreement on crawl rate, for a complete audit.",
)  # fmt: skip

PAGES_DENIED = Rule(
    "pages-access-denied", C, Severity.INFO, "Pages refused the auditor (401/403/429)",
    "Not necessarily a defect: bot protection or authentication often answers automated "
    "requests this way. These pages were not audited; allow the auditor (for a site you own) "
    "or audit them another way before drawing conclusions about them.",
)  # fmt: skip

RULES = (
    START_URL_FAILED, HTTPS_NOT_USED, MIXED_CONTENT, REDIRECT_CHAIN, PAGE_FETCH_ERROR,
    CRAWLED_PAGE_ERROR, CLIENT_RENDERED, NOINDEX_START, NOINDEX_PAGE, NOFOLLOW_PAGE,
    PAGE_TOO_LARGE, CRAWL_LIMIT, CRAWL_STOPPED, PAGES_DENIED,
)  # fmt: skip

_ERROR_IGNORED_STATUSES = frozenset({401, 403, 429})  # access control / rate limit, not "broken"


def analyze(ctx: AuditContext) -> list[Issue]:
    issues: list[Issue] = []
    issues += _start_url(ctx)
    for page in ctx.crawl.pages:
        issues += _fetch_issues(ctx, page)
    issues += _crawled_errors(ctx)
    issues += _client_rendered(ctx)
    for page in ctx.pages:
        issues += _page_issues(ctx, page)
    issues += _coverage_notes(ctx)
    return issues


# --------------------------------------------------------------------------- #


def _start_url(ctx: AuditContext) -> list[Issue]:
    start = ctx.start_page
    if start is None:
        reason = ctx.crawl.skipped.get(ctx.start_url)
        if reason is None:
            return []
        explanation = {
            "robots-disallowed": "robots.txt appears to disallow it for this auditor "
            "(re-run with --ignore-robots to audit a site you own anyway)",
            "robots-unavailable": "robots.txt could not be retrieved (see the robots.txt "
            "finding), and RFC 9309 tells crawlers to then assume a complete disallow",
            "non-html-resource": "its file extension indicates a non-HTML resource",
        }.get(reason, reason)
        transient = reason == "robots-unavailable" and ctx.crawl.robots.state == "unavailable"
        return [START_URL_FAILED.issue(
            description=f"The start URL was not fetched because {explanation}. This is an "
                        "incomplete audit, not an audited empty site.",
            evidence=f"Skipped with reason: {reason}.",
            confidence=0.6 if transient else 0.95, affected_urls=(ctx.start_url,),
            prevalence=1.0, details={"reason": reason},
        )]  # fmt: skip

    issues: list[Issue] = []
    if start.content is None:
        transient = start.error_kind in TRANSIENT_ERROR_KINDS or start.status_code in (429, 503)
        if start.error:
            evidence = f"Request failed ({start.error_kind}): {start.error}"
        elif not start.is_success:
            evidence = f"HTTP {start.status_code} from {start.final_url}"
        elif not same_origin(start.final_url, ctx.site_url):
            evidence = (
                f"Redirected to a different site: {start.final_url}. Audit that URL directly "
                "if it is the intended site."
            )
        else:
            evidence = f"Response is not HTML (Content-Type: {start.content_type or 'missing'})"
        issues.append(START_URL_FAILED.issue(
            description="The start URL did not return an auditable HTML page, so the audit is "
                        "incomplete"
                        + (" (the failure may be temporary; retry later)." if transient else "."),
            evidence=evidence, confidence=0.6 if transient else 0.95,
            affected_urls=(ctx.start_url,), prevalence=1.0,
            details={"status": start.status_code, "error": start.error,
                     "error_kind": start.error_kind, "final_url": start.final_url},
        ))  # fmt: skip
        return issues

    if start.final_url.startswith("http://"):
        local = is_local_host(host_of(start.final_url))
        issues.append(HTTPS_NOT_USED.issue(
            description="The start URL resolves to a plain-HTTP page."
                        + (" The host is a local/private address, so this is expected during "
                           "development; check the production site instead." if local else ""),
            evidence=f"Final URL {start.final_url} uses http://"
                     + (f" (requested {start.requested_url})." if start.redirect_chain else "."),
            confidence=0.95, affected_urls=(start.final_url,),
            severity=Severity.INFO if local else None,
            prevalence=1.0,  # transport security is a site-wide property
        ))  # fmt: skip
    if start.noindex_for_any_agent:
        issues.append(NOINDEX_START.issue(
            description=_noindex_description(start, "The start page"),
            evidence=robots_directive_evidence(start),
            confidence=0.95, affected_urls=(start.final_url,),
            details={"noindex_for": _noindex_scope(start)},
        ))  # fmt: skip
    return issues


def _fetch_issues(ctx: AuditContext, page: Page) -> list[Issue]:
    issues: list[Issue] = []
    if len(page.redirect_chain) >= 2:
        hops = " -> ".join(f"{hop.url} ({hop.status_code})" for hop in page.redirect_chain)
        issues.append(REDIRECT_CHAIN.issue(
            description=f"{page.requested_url} takes {len(page.redirect_chain)} redirects "
                        "to resolve.",
            evidence=f"{hops} -> {page.final_url}",
            confidence=0.97, affected_urls=(page.requested_url,),
            details={"hops": [{"url": h.url, "status": h.status_code} for h in page.redirect_chain],
                     "final_url": page.final_url},
        ))  # fmt: skip
    if page.error_kind == "too-large":
        issues.append(PAGE_TOO_LARGE.issue(
            description="The response exceeded the size limit, so the page was not analyzed "
                        "(a partial document would produce false findings).",
            evidence=page.error or "response too large",
            confidence=1.0, affected_urls=(page.final_url,),
        ))  # fmt: skip
    elif page.error and not ctx.is_start(page):
        transient = page.error_kind in TRANSIENT_ERROR_KINDS
        issues.append(PAGE_FETCH_ERROR.issue(
            description=f"{page.requested_url} returned no usable response"
                        + (" (possibly a temporary network failure)." if transient else "."),
            evidence=page.error,
            confidence=0.6 if transient else 0.9, affected_urls=(page.requested_url,),
            details={"error_kind": page.error_kind},
        ))  # fmt: skip
    return issues


def _crawled_errors(ctx: AuditContext) -> list[Issue]:
    """Link-discovered pages answering 4xx/5xx (sitemap URLs: see ``sitemap-url-error``)."""
    crawled = [p for p in ctx.crawl.pages if not p.error]
    failing = [
        p
        for p in crawled
        if p.discovered_via == "link"
        and p.status_code is not None
        and p.status_code >= 400
        and p.status_code not in _ERROR_IGNORED_STATUSES
    ]
    if not failing:
        return []
    share = len(failing) / max(1, len(crawled))
    # Escalate when most of what was crawled is broken: that is a site-wide failure.
    severity = Severity.CRITICAL if share >= 0.5 and len(failing) >= 3 else None
    entries = [f"{p.requested_url} (HTTP {p.status_code})" for p in failing]
    return [CRAWLED_PAGE_ERROR.issue(
        description=f"{len(failing)} of {len(crawled)} crawled pages returned an HTTP error.",
        evidence=joined_list(entries),
        confidence=0.95 if all(p.status_code in (404, 410) for p in failing) else 0.75,
        affected_urls=[p.requested_url for p in failing], severity=severity, prevalence=share,
        details={"statuses": {p.requested_url: p.status_code for p in failing}},
    )]  # fmt: skip


def _client_rendered(ctx: AuditContext) -> list[Issue]:
    shells = [p for p in ctx.pages if p.likely_client_rendered]
    if not shells:
        return []
    first = shells[0]
    assert first.content is not None
    return [CLIENT_RENDERED.issue(
        description=f"{plural(len(shells), 'page')} appear to be client-rendered application "
                    "shells: the server HTML has little content and relies on JavaScript. This "
                    "auditor does not run JavaScript, and neither do some crawlers. Findings "
                    "about missing content on these pages are reported with reduced confidence.",
        evidence=f"{first.final_url}: {first.content.visible_text_length} characters of visible "
                 f"text; signals: {', '.join(first.content.render_signals)}",
        confidence=0.9, affected_urls=[p.final_url for p in shells],
        details={"signals": {p.final_url: list(p.content.render_signals)
                             for p in shells if p.content}},
    )]  # fmt: skip


def _page_issues(ctx: AuditContext, page: Page) -> list[Issue]:
    assert page.content is not None
    url = (page.final_url,)
    issues: list[Issue] = []
    if page.final_url.startswith("https://"):
        insecure = [u for u in page.content.subresources if u.startswith("http://")]
        if insecure:
            issues.append(MIXED_CONTENT.issue(
                description=f"{plural(len(insecure), 'subresource')} load over http://.",
                evidence=f"Insecure subresources: {joined_list(insecure)}",
                confidence=0.95, affected_urls=url, details={"resources": sample(insecure, 20)},
            ))  # fmt: skip
    if page.noindex_for_any_agent and not ctx.is_start(page):
        issues.append(NOINDEX_PAGE.issue(
            description=_noindex_description(page, "The page"),
            evidence=robots_directive_evidence(page), confidence=0.95, affected_urls=url,
            details={"noindex_for": _noindex_scope(page)},
        ))  # fmt: skip
    if "nofollow" in page.robots_directives:
        issues.append(NOFOLLOW_PAGE.issue(
            description="The page asks all crawlers not to follow its links.",
            evidence=robots_directive_evidence(page), confidence=0.95, affected_urls=url,
        ))  # fmt: skip
    return issues


def _coverage_notes(ctx: AuditContext) -> list[Issue]:
    issues: list[Issue] = []
    denied = [p for p in ctx.crawl.pages if p.status_code in UNRETRIEVED_STATUSES]
    if denied:
        entries = [f"{p.requested_url} (HTTP {p.status_code})" for p in denied]
        issues.append(PAGES_DENIED.issue(
            description=f"{plural(len(denied), 'crawled page')} answered with an access or "
                        "rate-limit status, so their content was not audited. They count as "
                        "unmeasured in the coverage, not as passing.",
            evidence=joined_list(entries), confidence=1.0,
            details={"statuses": {p.requested_url: p.status_code for p in denied}},
        ))  # fmt: skip
    pending = ctx.crawl.pending
    if pending and not ctx.crawl.stop_reason:
        issues.append(CRAWL_LIMIT.issue(
            description=f"The crawl stopped after {ctx.crawl.config.max_pages} pages with "
                        f"{plural(len(pending), 'discovered URL')} not yet visited. Findings "
                        "describe the crawled subset only.",
            evidence=f"Not crawled: {joined_list(pending)}",
            confidence=1.0, details={"pending": len(pending), "sample": sample(pending, 20)},
        ))  # fmt: skip
    if ctx.crawl.stop_reason:
        reason = ctx.crawl.stop_reason
        if reason == "rate-limited":
            reason = "the server answered HTTP 429 (Too Many Requests)"
        elif reason == "crawl-delay-too-large":
            reason = (
                f"robots.txt asks for a Crawl-delay of {ctx.crawl.crawl_delay or 0:g}s, above "
                f"the {ctx.crawl.config.max_crawl_delay:g}s the auditor will wait, so only "
                "the start page was fetched"
            )
        issues.append(CRAWL_STOPPED.issue(
            description=f"The crawl stopped early because {reason}. Findings describe the "
                        "pages fetched before stopping.",
            evidence=f"stop reason: {ctx.crawl.stop_reason}; {len(pending)} URL(s) not crawled",
            confidence=1.0, details={"stop_reason": ctx.crawl.stop_reason},
        ))  # fmt: skip
    return issues


# --------------------------------------------------------------------------- #


def _noindex_scope(page: Page) -> list[str]:
    return ["all crawlers"] if page.is_noindex else list(page.noindex_agents)


def _noindex_description(page: Page, subject: str) -> str:
    if page.is_noindex:
        return f"{subject} asks all crawlers not to index it."
    names = {"googlebot": "Googlebot", "bingbot": "Bingbot"}
    agents = " and ".join(names.get(a, a) for a in page.noindex_agents)
    verb = "is" if len(page.noindex_agents) == 1 else "are"
    return f"{agents} {verb} explicitly instructed not to index {subject.lower()}."


def robots_directive_evidence(page: Page) -> str:
    parts = []
    if page.content:
        parts += [f'<meta name="{m.name}" content="{m.content}">' for m in page.content.robots_meta]
    if page.x_robots_tag:
        parts.append(f"X-Robots-Tag: {page.x_robots_tag.replace(chr(10), ' | ')}")
    return " and ".join(parts) or "robots directives present"
