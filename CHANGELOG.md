# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/) (pre-1.0: a minor bump may be
incompatible). The JSON report has its own `schema_version` (see
`docs/report-format.md`), and the Agent Skill declares the CLI and schema versions
it supports.

## 0.2.0 - Phase 01 hardening

A correctness and security release from two independent adversarial audits
(audit #1: 2 critical, 5 high, 9 medium, 3 low; audit #2: 2 high, 6 medium).
**Breaking:** report schema 2.x (2.1; see `docs/report-format.md` for the full
list of changes).

### Audit #2 fixes
- **Malformed input never crashes the audit (N1).** A malformed URL in a link,
  image, `<base>`, canonical, `Link` header or redirect `Location`
  (`http://[bad`), or an unusable charset (`charset=bogus`, `hex`) previously
  raised out of the crawl (no report, exit 1) or made the whole page
  "unparseable". Such URLs are now invalid values (`malformed-canonical`,
  `invalid-redirect`), unusable charsets fall back to UTF-8, and an unexpected
  internal error exits with code 3, a clear message, and no stale report.
- **What was not observed is never "checked" (N2).** Pages that could not be
  retrieved or analyzed (network/TLS/protocol errors, SSRF blocks, unsupported
  encodings, oversized or unparseable bodies, HTTP 401/403/407/429) make the
  crawl `partial` and are listed in `crawl.unretrieved`. Link targets count as
  checked only with a definitive answer. A category with fewer measured than
  unmeasured items is N/A, and the overall score is withheld
  (`insufficient-coverage`) when less than half the model could be scored. Before:
  bot protection refusing 19 of 20 pages produced "complete, 92/100, links 20/20".
  New info rule `pages-access-denied`.
- **robots.txt wildcard ReDoS (N3).** Patterns are matched in linear time instead
  of with a backtracking regex that a hostile `Disallow: /*a*a*a*...` rule plus a
  crafted link could make run for hours.
- **Client-rendered shells are not scored from nothing (N4).** Page-level
  categories are N/A when every audited page is a shell; the score is withheld.
- **Sitemap XML is streamed with a depth limit (N5).** Hostile nesting (no DTD
  needed) cost ~80x its size in memory (7 MB -> 563 MB); now rejected at depth 32
  with ~2 MB peak. Large legitimate sitemaps use ~3.5x less memory.
- **Bounded text reads logos correctly (N6).** The 64-node text budget is no longer
  spent inside `<svg>`, `<script>`, `<style>` or `<template>`, so
  `<h1><svg>40 paths</svg>Acme</h1>` is not reported as an empty heading or link.
- **Install ref (N7).** The documented `@v0.2.0` tag is created with this release;
  a test keeps every documented install ref equal to the package version, and CI
  checks that a pushed tag matches the version.
- **Crawl-delay above the limit means start page only (N8).** Link checks and
  sitemap fetches no longer continue at 30 s intervals (up to ~50 minutes); the
  remaining link targets are listed as unchecked.

### Robots
- robots.txt is evaluated **per crawler** (Googlebot, Bingbot, GPTBot,
  OAI-SearchBot, ClaudeBot, PerplexityBot, Google-Extended, CCBot) with the
  deciding group and rule. A `*` block with an explicit Googlebot exemption is no
  longer reported as "disallow all". A Googlebot-only block is no longer missed. (C1)
- New rules: `robots-search-crawler-blocked`, `robots-ai-crawler-blocked` (info),
  `robots-txt-rate-limited`. `robots-disallow-all` now means *every evaluated
  search crawler* is blocked, and `Disallow: /$` no longer counts as "everything".
- robots.txt retrieval states: 429, 5xx and network failures mean **no crawling**,
  not "missing". Transient failures are retried once. (M1)
- Percent-encoding is normalized consistently for rules and URLs (`/%7Ejoe` = `/~joe`). (L2)
- `Crawl-delay` honoured (up to 30 s; above that, only robots.txt and the start
  page are requested). (M8, N8)

### Parsing and rendering
- Multi-signal detection of client-rendered shells (`client-rendered-shell`).
  "Missing" findings on such pages are capped at confidence 0.35 and marked
  `render_dependent`. (C2)
- `googlebot`/`bingbot` meta tags and agent-scoped `X-Robots-Tag` are evaluated
  per agent (`noindex_for`). (H2)
- Canonicals from the HTTP `Link` header. Canonicals in `<body>` or after an
  implicit `</head>` are reported (`canonical-outside-head`) rather than trusted.
  New: `canonical-header-conflict`. (M2)

### Crawler and network security
- **Bounded decompression**: bodies are read raw and decompressed with a bounded
  output size. A 200 MB gzip bomb now peaks at about 3 MB (previously about 141 MB
  for 60 MB). Oversized pages are not analyzed (`page-too-large`, which replaces
  `page-truncated`). New: `--max-page-bytes`. (H1)
- **DNS pinning**: each hop connects to the validated IP (Host header and SNI
  preserved), closing the DNS-rebinding window. DNS resolution obeys the timeout.
  Embedded-IPv4 IPv6 forms (NAT64, 6to4, Teredo) are judged by the embedded
  address. Environment proxies are ignored. (M5)
- `Retry-After` on 429/503 honoured once (≤ 30 s); otherwise a 429 stops the
  crawl (`crawl-stopped-early`). Sleep and clock are injectable. (M8)
- Total body deadline is enforced per received chunk (it previously relied on
  64 KB re-buffering).
- HTML extraction walks the tree once with bounded per-element text, so deeply
  nested hostile markup costs linear rather than quadratic time.
- Same-site redirect adoption is limited to the same host ignoring `www.`. A
  subdomain move is no longer adopted (`user.github.io` ≠ `github.io`). (L1)

### Sitemaps
- The guessed `/sitemap.xml` cannot produce defects from a 404 or an HTML (SPA
  fallback) response. (H5)
- `sitemap.complete`. `page-not-in-sitemap` is skipped on incomplete lists and
  only lists indexable, self-canonical, crawlable pages. New: `sitemap-incomplete`. (M3)
- Empty sitemap files are aggregated into a single `sitemap-empty` finding. (M3)
- XML parsed with `defusedxml` (DTDs and entities rejected anywhere, not only in
  the first 4 KB). (M4)

### Scoring
- **Coverage-aware score**: categories are `scored`, `partial` or `not-scored`
  (N/A). Unchecked links are N/A, never 20/20. The overall score is normalized
  over scored categories, and `score.status` is reported. (H3)
- The per-rule penalty cap was removed (dozens of failures now cost proportionally).
  Broken-link prevalence is relative to checked targets. Severity weights are now
  1.0 / 0.75 / 0.4 / 0.15 / 0.
- New `crawled-page-error` (critical when at least half the crawled pages fail).
- Crawl `state` (`complete`/`partial`/`failed`). A failed crawl is reported as an
  incomplete audit with no score.

### Data model
- Domain models moved to `web_visibility.models` (page, issue, robots, sitemap,
  crawl) and configuration to `web_visibility.config`. Models no longer depend on
  the crawler. (M7)
- `Issue` gained `verification`, plus optional `impact` and `effort` (left unset
  rather than guessed).

### CLI and skill
- `--version` reports the report schema version.
- SKILL.md: a standalone install (`pipx install …@v0.2.0`) separated from
  development setup, plus `requires-cli` / `report-schema` metadata and an
  explicit compatibility check. (H4)

### Tests and CI
- End-to-end regression suite for every finding (`tests/integration/test_hardening.py`),
  a golden finding set for the fixture site, SPA fixtures, SSRF pinning and
  rebinding tests, parser complexity tests, and a score sanity matrix. 297 → 425 tests (571 after audit #2).
- Audit #2: `tests/regression/` (each N finding plus the auditor's probe matrices:
  robots semantics, JS shells, canonicals, crawl failures, Retry-After) and
  `tests/security/` (SSRF, decompression bombs, hostile XML, ReDoS), all over real
  local sockets via `tests/live_server.py`.
- `scripts/mutation_check.py` reverts each audit fix in a scratch copy and fails if
  the suite does not notice; CI runs it.
- CI: actions pinned to commit SHAs, Python 3.11–3.14, a workflow validation test,
  a release-tag/version check, and the mutation job. (M9) The first GitHub run (before
  these changes) failed: mypy errors since fixed in `parsing.py`, and one CLI test
  that broke when Rich styles help text under `GITHUB_ACTIONS` (now ANSI-insensitive).

### Documentation
- SECURITY.md rewritten with precise guarantees and residual risks. The README
  gained known limitations, score explanation, robots, sitemap and security
  sections. Check counts corrected. (L3)

## 0.1.0

Initial Phase 01 foundation.

- `web-visibility audit <URL>` CLI with terminal, JSON and Markdown output.
- Bounded same-origin crawler with an SSRF guard, robots.txt and sitemap support.
- Deterministic checks across technical SEO, crawlability, metadata, canonicals,
  headings, links, images and JSON-LD. *(The release notes said "73 checks"; that
  count included informational notes.)*
- Evidence-backed issue model, diagnostic score, Agent Skill, fixture site, tests, CI.
