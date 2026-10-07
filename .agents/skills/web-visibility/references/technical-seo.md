# Technical SEO reference (Phase 01, v0.2)

This file explains every check the auditor performs: what it inspects, which
rule IDs it can emit, and how far a finding can be trusted. Read the section for
a rule ID before explaining or acting on it.

**Ground rules for every section:**

- A finding describes an *observed condition* in the server responses, backed by
  evidence. It is not a prediction of how any search engine will rank or index the site.
- Severities: `critical` (site-wide blocker), `high`, `medium`, `low`,
  `info` (context only, never deducted from the score).
- `confidence` (0–1) says how sure the auditor is that the condition is real
  *and* is a problem. Heuristics (title length, image dimensions, orphans),
  transient failures (timeouts) and findings on client-rendered pages are
  deliberately low-confidence.
- `crawl.state` is `complete`, `partial` (findings cover a subset) or `failed`
  (nothing could be audited). A failed crawl is an **incomplete audit**, never
  evidence of an empty or broken site.

Contents: [Crawlability](#1-crawlability) · [Rendering](#2-client-rendered-pages) ·
[Indexability](#3-indexability) · [robots.txt](#4-robotstxt) ·
[XML sitemaps](#5-xml-sitemaps) · [Canonical URLs](#6-canonical-urls) ·
[HTTP status](#7-http-status-codes) · [Redirects](#8-redirects) ·
[HTTPS](#9-https-and-mixed-content) · [Titles](#10-titles) ·
[Meta descriptions](#11-meta-descriptions) · [Duplicate metadata](#12-duplicate-metadata) ·
[Headings](#13-headings) · [Internal links](#14-internal-links) ·
[External links](#15-external-links) · [Images](#16-images-and-alt-text) ·
[Structured data](#17-structured-data) · [Crawl coverage](#18-crawl-coverage-and-limits) ·
[Scoring](#19-scoring)

---

## 1. Crawlability

**What it is.** Whether a crawler can reach a page (via links or sitemaps)
without being blocked, and receive a usable response.

**What the auditor checks.** It fetches robots.txt, then the start URL. If the
start URL redirects to the same host (http → https, adding or removing `www.`),
that origin becomes the crawl origin (`crawl.site_url`). Redirects to other
hosts, including subdomains, are not followed for crawling. The crawler then
follows same-origin links breadth-first, then sitemap URLs, up to `--max-pages`.
Fetch failures record their cause (`timeout`, `connection`, `dns`, `ssl`,
`too-many-redirects`, `invalid-redirect`, `too-large`, `unsupported-encoding`, `blocked`). robots.txt
and the start URL are retried once after a transient network failure.

| Rule ID | Severity | Finding |
|---|---|---|
| `start-url-unavailable` | critical (confidence 0.6 if the cause may be temporary) | The start URL produced no auditable HTML page: request failed, non-2xx, non-HTML, redirected to a different site, or skipped (robots, oversized). No score is computed; the audit is incomplete. |
| `page-fetch-error` | medium (confidence 0.6 for timeouts/connection errors) | A crawled URL returned no usable response. |

**Do not assume.** That a timeout means the page is down for everyone, or that
pages the crawler didn't reach don't exist.

## 2. Client-rendered pages

**What it is.** Single-page applications (React, Vite, Angular, …) often serve
an HTML *shell*, an empty mount element plus scripts, and build the content in
the browser. The auditor does **not** execute JavaScript.

**What the auditor checks.** It combines several signals rather than relying on
`id="root"`: visible text under 150 characters, script-driven markup, an empty
mount element (`#root`, `#app`, `#__next`, `data-reactroot`, `<app-root>`, …),
near-empty body containers (catches custom ids), few content elements, and a
`<noscript>` "enable JavaScript" notice. A page is flagged only when it has
little text, is script-driven, **and** shows at least one structural signal.

| Rule ID | Severity | Finding |
|---|---|---|
| `client-rendered-shell` | medium (confidence 0.9) | The server response is an application shell with little server-rendered content. Browser rendering may be required for a complete audit. |

**Confidence model.** On flagged pages, findings about something being *absent*
that rendering could add (`missing-title`, `empty-title`, `short-title`,
`missing-meta-description`, `missing-canonical`, `missing-h1`,
`no-internal-outlinks`, `duplicate-title`, `duplicate-meta-description`, and
site-wide `no-structured-data` / `potential-orphan-page`) are kept, because they
are true of the server HTML. Their confidence is capped at **0.35**, they are
marked `details.render_dependent = true`, and their description says so. Page
categories are scored as `partial`.

**Do not assume.** That a shell means "SEO is broken". Some crawlers render
JavaScript and some do not. The defensible claim is: *the server response
contains limited server-rendered content, and browser rendering may be required
to see the rest*.

## 3. Indexability

**What it is.** Page-level directives asking crawlers not to index or follow:
`<meta name="robots">`, `<meta name="googlebot">`, `<meta name="bingbot">`, and
`X-Robots-Tag` headers, including agent-scoped values (`googlebot: noindex`).

**What the auditor checks.** It collects directives for all crawlers and for
`googlebot` and `bingbot` individually. An agent-scoped header line applies only
to that agent, and other agents' scopes never leak. `none` means `noindex,
nofollow`. `noarchive` and `nosnippet` are recorded in `pages[].robots_directives`
but are not findings.

| Rule ID | Severity | Finding |
|---|---|---|
| `noindex-start-page` | high | The start page is noindex for all crawlers, or for Googlebot/Bingbot specifically (`details.noindex_for`). Wording: "Googlebot is explicitly instructed not to index the start page." |
| `noindex-page` | info | Another page is noindex (often intentional). |
| `nofollow-page` | info | Page-level nofollow addressed to all crawlers. |

**Do not assume.** That a noindex page "cannot appear in Google", or that it is a
mistake. Ask, or check whether it is a utility page.

## 4. robots.txt

**What it is.** The plain-text file at `/robots.txt` that tells crawlers which
paths they may fetch (RFC 9309). It controls *crawling*, not indexing.

**Retrieval states** (`robots.state`) and how the auditor behaves:

| Response | State | Auditor behaviour |
|---|---|---|
| 2xx | `parsed` | Rules applied |
| 404, 410, other 4xx | `missing` | No restrictions (RFC 9309) |
| 401, 403 | `inaccessible` | No restrictions (RFC 9309), reported |
| 429 | `rate-limited` | **Does not crawl** (treated like a server error) |
| 5xx | `server-error` | **Does not crawl** (RFC 9309: assume complete disallow) |
| timeout / DNS / network | `unavailable` | **Does not crawl** after one retry |

Failures bias towards *not* crawling, never towards unrestricted access.

**Per-crawler evaluation.** Every audit evaluates these tokens: Googlebot,
Bingbot (search); GPTBot, OAI-SearchBot, ClaudeBot, PerplexityBot,
Google-Extended, CCBot (AI); plus the auditor's own token `WebVisibilitySkill`
(which the crawler obeys). Group selection uses an exact, case-insensitive
product-token match (`Googlebot/2.1` matches `googlebot`). Groups naming the same
agent are merged, and `*` applies only when no group names the agent. Within a
group the longest matching rule wins and `Allow` wins ties. `*` and `$` wildcards
are supported, an empty `Disallow` allows everything, and percent-encoding is
normalized (`/%7Ejoe` = `/~joe`). `robots.crawlers` in the JSON report lists each
token's verdict and deciding rule.

| Rule ID | Severity | Finding |
|---|---|---|
| `robots-txt-missing` | low | No robots.txt. A file is optional. |
| `robots-txt-access-denied` | medium | 401/403. |
| `robots-txt-rate-limited` | medium | 429. The auditor did not crawl. |
| `robots-txt-unreachable` | high (confidence 0.6 for network failures) | 5xx or network failure. |
| `robots-txt-not-plain-text` | medium | The response looks like an HTML page. |
| `robots-disallow-all` | critical | **Every** evaluated search crawler is blocked from the whole site (the deciding rule matches all paths, `/` or `/*`, and the group has no Allow rules). Expected on staging sites. |
| `robots-search-crawler-blocked` | high | One search crawler (e.g. only Googlebot, or Bingbot via `*` while Googlebot is exempted) is blocked from the whole site. |
| `robots-ai-crawler-blocked` | info | AI crawler tokens are blocked from the whole site. This is usually a deliberate policy. |
| `start-url-disallowed` | high | The start URL is disallowed for a search crawler (without a whole-site block). `Disallow: /$` lands here, not under disallow-all. |
| `crawled-page-disallowed` | medium | Pages crawled with `--ignore-robots` that search crawlers may not fetch. |
| `linked-url-disallowed` | info | Internal links point to URLs the auditor was disallowed from. |
| `sitemap-url-disallowed` | medium | The sitemap lists URLs that search crawlers may not fetch. |

**Not modelled:** engine-specific fallbacks (`googlebot-image` → `googlebot`),
`Host` and `Clean-param`. `Crawl-delay` is honoured by the auditor itself (see §18).

**Do not assume.** That a disallowed URL won't appear in search results (robots
blocks crawling, not indexing). Never write "Google will never index this page".
Write "appears disallowed for Googlebot by the site's robots.txt rules".

## 5. XML sitemaps

**What it is.** Files listing URLs the site wants crawled (sitemaps.org protocol):
`<urlset>`, `<sitemapindex>`, gzip, or plain text.

**Declared vs guessed.** Sitemaps declared in robots.txt (`source: robots`) or
in a sitemap index (`source: index`) can produce defects. `/sitemap.xml` is also
tried, but only as a **guess** (`source: default`). A 404 or an HTML page there
(an SPA or CMS catch-all) is *not* a defect. It only counts if it is clearly
meant to be a sitemap: it parses, or it is non-HTML XML that fails to parse.

**Completeness.** `sitemap.complete` is false if the file limit (10) or URL limit
(50,000) was reached, or a declared or indexed file could not be read. Then
`page-not-in-sitemap` is skipped, because it would be guesswork on a partial list.

**XML safety.** Parsed with `defusedxml`, which rejects any DTD, entity declaration
or external reference anywhere in the document.

| Rule ID | Severity | Finding |
|---|---|---|
| `sitemap-missing` | low | No valid sitemap was declared or found at `/sitemap.xml`. Sitemaps are optional. The evidence explains what each location returned. |
| `sitemap-inaccessible` | medium | A declared or indexed sitemap could not be fetched. |
| `sitemap-invalid` | medium (confidence 0.8 for the guessed location) | Not parseable: malformed or unsafe XML, wrong root, invalid gzip, or over 50 MB. |
| `sitemap-empty` | low | Sitemap files with no URLs, **aggregated** into one finding listing the files. |
| `sitemap-duplicate-urls` | low | The same URL is listed twice. |
| `sitemap-invalid-urls` | low | `<loc>` values that are not absolute http(s) URLs. |
| `sitemap-cross-host-urls` | low | URLs on another host than the sitemap file. |
| `sitemap-incomplete` | info | The list was not read completely, so dependent checks were skipped. |
| `sitemap-url-error` | medium | A listed URL that was crawled returned an error. |
| `sitemap-url-redirects` | low | A listed URL redirects. |
| `sitemap-url-noindex` | medium | A listed URL is noindex (for all crawlers or a specific one). Remove it from the sitemap *or* drop the noindex. |
| `sitemap-url-non-canonical` | low | A listed URL declares a different canonical. |
| `page-not-in-sitemap` | info | Crawled pages that are indexable, self-canonical and crawlable but missing from a *complete* sitemap. These never overlap with the "remove from sitemap" findings above. |
| `sitemap-not-in-robots` | info | A sitemap exists at `/sitemap.xml` but robots.txt doesn't reference it. |

## 6. Canonical URLs

**What it is.** The preferred URL among duplicates, declared with
`<link rel="canonical">` or the HTTP header `Link: <url>; rel="canonical"`.

**Precedence.** A canonical in `<head>` comes first, then the HTTP header.
Canonicals in `<body>`, or after an element that makes parsers end `<head>` early
(a `<div>` or `<img>` inside `<head>`), are **not** used as the page's canonical.
They are reported as placement problems.

| Rule ID | Severity | Finding |
|---|---|---|
| `missing-canonical` | low | No head or header canonical on an indexable page (not reported when a misplaced canonical exists). |
| `multiple-canonicals` | medium (low if identical) | More than one `<head>` canonical. |
| `canonical-header-conflict` | medium | The HTML and HTTP header canonicals disagree. |
| `canonical-outside-head` | medium (confidence 0.9 in body; 0.5 after an implicit `</head>`) | Canonical in `<body>`, or written in `<head>` after body-only content. Parser behaviour varies in the second case, so confidence is low. |
| `malformed-canonical` | medium | No `href`, or not an http(s) URL. |
| `canonical-target-error` | medium | The canonical URL returns 4xx/5xx or failed. |
| `canonical-target-redirects` | low | The canonical URL redirects. |
| `canonical-cross-host` | info | The canonical points to another host (syndication is legitimate). |
| `canonicalized-to-other-url` | info | The page names a different URL as canonical. |
| `canonical-noindex-conflict` | low | noindex combined with a canonical to another URL. |

**Do not assume.** That the auditor emulates Google's HTML parser exactly. It
detects suspicious placement and lowers confidence instead.

## 7. HTTP status codes

Interpretation policy for crawled pages and link checks:

| Response | Interpreted as |
|---|---|
| 2xx (after redirects) | working |
| 404, 410 | broken (high confidence) |
| other 4xx | broken candidate (confidence 0.8) |
| 401, 403 | **inaccessible, not necessarily broken** → `unverified-link` |
| 429 | **rate limited**. The crawl stops after one `Retry-After` retry (§18) |
| 5xx | broken candidate, may be transient (confidence 0.6) |
| timeout / connection / DNS | **unverified**. Nothing is proven |

Link checks use `HEAD` and confirm failures with `GET`.

| Rule ID | Severity | Finding |
|---|---|---|
| `crawled-page-error` | high, **critical** when at least half of the crawled pages (and at least 3) fail | Link-discovered pages returning 4xx/5xx (401/403/429 excluded). One aggregated finding with prevalence = failing ÷ crawled pages. |

## 8. Redirects

The fetcher follows up to 5 hops manually, records each hop, detects loops, and
checks every hop against the SSRF guard.

| Rule ID | Severity | Finding |
|---|---|---|
| `redirect-chain` | low | Two or more hops before the final URL. |
| `internal-link-redirect` | low | Internal links point at a URL that redirects. |

## 9. HTTPS and mixed content

| Rule ID | Severity | Finding |
|---|---|---|
| `https-not-used` | high (info on localhost/private hosts) | The final start page is served over `http://`. Treated as site-wide. |
| `mixed-content` | medium | An HTTPS page loads `http://` subresources. |
| `insecure-internal-link` | low | An HTTPS site links to its own pages via `http://`. |

## 10. Titles

`<title>` in the document (titles inside inline `<svg>` are ignored).

| Rule ID | Severity | Finding |
|---|---|---|
| `missing-title` | high | No `<title>`. |
| `empty-title` | high | Empty `<title>`. |
| `multiple-titles` | low | More than one. |
| `short-title` | low (confidence 0.6) | Under 10 characters. |
| `long-title` | low (confidence 0.6) | Over 70 characters; may be truncated. |

**Do not assume.** That length thresholds are rules. Engines truncate by pixel
width and may rewrite titles.

## 11. Meta descriptions

| Rule ID | Severity | Finding |
|---|---|---|
| `missing-meta-description` | low | None present. |
| `empty-meta-description` | low | Present but empty. |
| `multiple-meta-descriptions` | low | More than one. |

**Do not assume.** Any exact character limit, or that a missing description hurts rankings.

## 12. Duplicate metadata

| Rule ID | Severity | Finding |
|---|---|---|
| `duplicate-title` | medium | One finding per duplicated value, listing every page. |
| `duplicate-meta-description` | low | Same, for descriptions. |

**False-positive guards.** Pages noindex for any evaluated crawler are excluded.
Groups whose pages share one canonical URL are consolidated, not duplicated.

## 13. Headings

| Rule ID | Severity | Finding |
|---|---|---|
| `missing-h1` | medium | No H1. |
| `multiple-h1` | low | More than one H1. This is valid HTML and is reported as a structural condition. |
| `heading-level-skip` | low | A level is skipped going down (H2 → H4). |
| `empty-heading` | low | Headings without text. |

## 14. Internal links

Each problematic target produces **one** finding listing every linking page and
the anchor texts. Prevalence is relative to the number of **checked** targets, so
19 broken targets out of 20 cost 19/20 of the category. Linked non-HTML files
(PDFs, images) are link-checked too.

| Rule ID | Severity | Finding |
|---|---|---|
| `broken-internal-link` | high (404/410), medium (other 4xx, 5xx, loops) | The target appears broken. `details.target` is the URL. |
| `unverified-link` | info | 401/403/429/timeout: not proven either way. |
| `empty-anchor-text` | low | Links without an accessible name. |
| `generic-anchor-text` | info | "click here", "read more", … |
| `no-internal-outlinks` | low | A page linking to no other page on the site. |
| `internal-nofollow` | info | Internal links with `rel="nofollow"`. |
| `potential-orphan-page` | low (confidence 0.7, or 0.5 on a partial crawl) | Found only via the sitemap, and no crawled page links to it. |
| `links-not-checked` | info | Targets left unverified (budget exhausted). |

## 15. External links

Recorded but never crawled. They are checked only with `--check-external` (up to 25).

| Rule ID | Severity | Finding |
|---|---|---|
| `broken-external-link` | low | The external target appears broken. |

## 16. Images and alt text

| Rule ID | Severity | Finding |
|---|---|---|
| `image-missing-alt` | medium | `alt` attribute **absent**. |
| `image-empty-alt` | info | `alt=""`, the correct markup for decorative images. Never a defect. |
| `image-missing-dimensions` | low (confidence 0.6) | `width`/`height` attributes absent (CSS is not evaluated). |

## 17. Structured data

JSON-LD is parsed. Microdata and RDFa are only detected.

| Rule ID | Severity | Finding |
|---|---|---|
| `invalid-json-ld` | medium | A block fails to parse. |
| `json-ld-missing-context` | low | Valid block without `@context`. |
| `json-ld-missing-type` | low | A top-level entity without `@type`. |
| `no-structured-data` | low | No JSON-LD, microdata or RDFa on any crawled page. This is not a critical failure. |
| `non-json-ld-structured-data` | info | Microdata/RDFa present but not analyzed. |

**Do not assume.** That valid JSON-LD is valid Schema.org (that is Phase 02).
Never recommend markup for information the page does not visibly contain.

## 18. Crawl coverage and limits

- **Size limit**: bodies are capped at 5 MB *decompressed* (`--max-page-bytes`).
  Raw bytes are decompressed by the auditor with bounded output, so a compressed
  "bomb" cannot expand in memory. Oversized pages are **not analyzed**, because a
  partial document would produce false "missing" findings.
- **Rate control**: a `Retry-After` on 429/503 of up to 30 s is honoured with one
  retry. A longer or repeated 429 stops the crawl. robots.txt `Crawl-delay` is
  honoured up to 30 s. Above that, only robots.txt and the start page are requested (no sitemaps, no link checks; the remaining link targets are listed as unchecked).

| Rule ID | Severity | Finding |
|---|---|---|
| `page-too-large` | low | The response exceeded the decompressed size limit and was not analyzed. |
| `crawl-limit-reached` | info | Stopped at `--max-pages` with URLs pending. Findings cover the crawled subset. |
| `crawl-stopped-early` | info | Stopped to respect the server (`rate-limited`, `crawl-delay-too-large`). |
| `pages-access-denied` | info | Crawled pages answered 401/403/429 (often bot protection). They were not audited and count as unmeasured, never as passing. |

**Unmeasured is never "checked".** A page the auditor could not observe (network,
TLS or protocol failure, SSRF block, unsupported encoding, too large, unparseable,
or 401/403/407/429) makes `crawl.state` `partial` and is listed in
`crawl.unretrieved`. A link target answering 401/403/429 or failing at the network
level is *unverifiable*: it is neither broken nor working. HTTP 404/410/5xx are
real observations and do not reduce coverage.

When `crawl.state` is `partial`, every site-wide conclusion needs that caveat.

## 19. Scoring

The **Web Visibility Diagnostic Score** is coverage-aware:

- Each category reports `coverage.status`: `scored`, `partial`, `not-scored`
  (applicable items exist but none was checked, **or fewer were measured than
  could not be measured**: links found but not verified, pages behind bot
  protection, client-rendered shells, a failed crawl) or `not-applicable`
  (nothing to check: no images, no links). Both N/A statuses are excluded from
  the overall score and never treated as full marks.
- Category weights: Technical 25, Metadata 20, Structure 15, Links 20,
  Images 10, Structured Data 10.
- Penalty per issue = `severity weight × confidence × prevalence`. Severity
  weights: critical 1.0, high 0.75, medium 0.4, low 0.15, info 0. Penalties add
  up to at most the whole category, so dozens of failures are never hidden
  behind one capped rule.
- Overall = 100 × scored points ÷ scored maximum. `score.status` is `complete`,
  `partial`, `insufficient-coverage` or `not-scored`. With
  `insufficient-coverage`, the scored categories carry less than
  `score.min_scored_weight` (50) of the model, so `overall` is `null`:
  report "partial audit, no score" and the `score.reason`. Never estimate one.

It is an internal diagnostic, **not** a ranking, traffic or AI-visibility prediction.
