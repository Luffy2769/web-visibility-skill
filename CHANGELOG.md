# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/) (pre-1.0: a minor bump may be
incompatible). The JSON report has its own `schema_version` (see
`docs/report-format.md`), and the Agent Skill declares the CLI and schema versions
it supports.

## 0.2.0 - Phase 01 hardening

A correctness and security release from an adversarial audit (2 critical,
5 high, 9 medium, 3 low findings). **Breaking:** report schema 2.0 (see
`docs/report-format.md` for the full list of changes).

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
- `Crawl-delay` honoured (up to 30 s; above that, only the start page is fetched). (M8)

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
  rebinding tests, and a score sanity matrix. 297 → 418+ tests.
- CI: actions pinned to commit SHAs, Python 3.11–3.14, a workflow validation test.
  **GitHub-hosted execution has not been verified yet.** (M9)

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
