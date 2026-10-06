# JSON report format (`schema_version` 2.0)

`web-visibility audit <url> --format json` (stdout) or `--output DIR`
(`DIR/<host>/audit.json`) produce this structure.

**Versioning policy.** `schema_version` is `MAJOR.MINOR`. The minor number changes
on additive changes (new fields, new rule IDs). The major number changes on
renames, removals or changed meanings. Consumers, including the Agent Skill,
must check the major number before interpreting a report.

**Changes from 1.0 to 2.0 (incompatible):** `crawl.state`, `score.status` and
per-category `coverage` were added. Category `score` can now be `null` (N/A). The
severity weights changed and the per-rule penalty cap was removed. `robots` gained
`state` and `crawlers`. Pages gained `canonicals[].source`, `robots_meta`,
`robots_directives` and `rendering`. Issues gained `verification`, `impact` and
`effort`. The skip reason `robots-unreachable` was renamed `robots-unavailable`,
and the rule `page-truncated` was replaced by `page-too-large`.

```jsonc
{
  "schema_version": "2.0",
  "project": "web-visibility",
  "version": "0.2.0",
  "generated_at": "2026-10-06T12:00:00+00:00",
  "target": { "url": "...", "normalized_url": "...", "final_url": "..." },
  "crawl": { ... },
  "score": { ... },
  "summary": { ... },
  "issues": [ ... ],
  "pages": [ ... ],
  "robots": { ... },
  "sitemap": { ... },
  "structured_data": { "types": { "Organization": 1 } },
  "limitations": [ "..." ]
}
```

## `crawl`

| Field | Type | Meaning |
|---|---|---|
| `state` | `complete` \| `partial` \| `failed` | `failed`: nothing could be audited (an **incomplete audit**, not an empty site). `partial`: findings cover a subset. |
| `state_reasons` | string[] | Why the state is not `complete`. |
| `start_url` / `site_url` | string | The URL asked for, and where the crawl ran (same-host redirect target). |
| `pages_crawled` / `pages_audited` / `pages_client_rendered` | int | Fetched URLs; analyzed HTML pages; pages flagged as client-rendered shells. |
| `max_pages`, `limit_reached`, `pending_urls`, `pending_sample` | | Page limit and what was left uncrawled. |
| `stop_reason` | string \| null | `rate-limited` or `crawl-delay-too-large`. |
| `crawl_delay_seconds` | number \| null | robots.txt Crawl-delay applied. |
| `skipped` | {url, reason}[] | `robots-disallowed`, `robots-unavailable`, `non-html-resource`. |
| `links_checked` / `links_unchecked`, `requests`, `duration_seconds` | | Cost and coverage of link checks. |
| `settings` | object | Options used (limits, timeouts, retry wait, User-Agent, private-network opt-in). |

## `score`

| Field | Type | Meaning |
|---|---|---|
| `name` | string | `"Web Visibility Diagnostic Score"`. |
| `status` | `complete` \| `partial` \| `not-scored` | Whether every category was fully covered. |
| `overall` | int \| null | 0–100 over the scored categories; `null` if none. |
| `scored_weight` | int | Sum of weights behind `overall` (100 = every category scored). |
| `disclaimer` | string | Not a ranking, traffic or AI-visibility prediction. |
| `categories.<key>.score` | number \| null | `null` = **N/A** (not scored). |
| `categories.<key>.coverage` | object | `status` (`scored`/`partial`/`not-scored`/`not-applicable`), `unit`, `applicable`, `checked`, `failed`, `reason`. |
| `categories.<key>.deductions[]` | object | `rule_id`, `severity`, `issues`, `penalty` (fraction of the category), `points`. |
| `model` | object | Severity weights and formula. |

Category keys: `technical`, `metadata`, `structure`, `links`, `images`, `structured_data`.

## `issues[]`

Sorted by severity, then number of affected URLs, then ID.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Stable rule ID (see `references/technical-seo.md`). |
| `category`, `severity`, `title`, `description` | string | |
| `evidence` | string | The concrete observation. Never empty. |
| `affected_url` / `affected_urls` | string \| null / string[] | Where it was observed. An empty list means site-wide. |
| `recommendation` | string | Suggested change. |
| `verification` | string | How to confirm the fix. |
| `confidence` | number | 0–1. Capped at 0.35 for `details.render_dependent` findings. |
| `prevalence` | number \| null | Explicit scoring prevalence, if set. |
| `impact` / `effort` | int \| null | 1–10. `null` in Phase 01 (not estimated rather than guessed). |
| `details` | object | Rule-specific data (`target`, `status`, `crawler`, `noindex_for`, `placement`, `render_dependent`, …). |

## `pages[]`

Always present: `url`, `requested_url`, `status_code`, `content_type`,
`discovered_via` (`start`/`link`/`sitemap`), `depth`, `audited`,
`redirect_chain[]`, `error`, `error_kind` (`timeout`, `connection`, `ssl`, `dns`,
`blocked`, `redirect-loop`, `too-many-redirects`, `too-large`,
`unsupported-encoding`, `http`, `parse`), `truncated`, `response_headers`
(`set-cookie` redacted), `issue_ids[]`.

Parsed HTML pages also have: `title`, `meta_description`, `canonical`
(effective: `<head>` first, then HTTP header), `canonicals[]` (`href`, `source`:
`head`/`head-implicitly-closed`/`body`/`http-header`), `robots_meta[]`
(`name`, `content`), `x_robots_tag`, `noindex` (all crawlers),
`robots_directives` (`all`, `googlebot`, `bingbot`), `lang`, `rendering`
(`likely_client_rendered`, `visible_text_length`, `signals[]`),
`head_anomalies[]`, `h1s[]`, `headings[]`, `links`, `inbound_internal_links`,
`images`, `structured_data[]`, `has_microdata`, `has_rdfa`.

## `robots`

`url`, `state` (`parsed`, `missing`, `inaccessible`, `rate-limited`,
`server-error`, `unavailable`, `blocked`), `exists`, `restricts_crawling`,
`status_code`, `content_type`, `error`, `sitemaps[]`, `groups[]`
(`user_agents[]`, `rules[]` of `{allow, path}`, `crawl_delay`), `content`, and
`crawlers[]`: one entry per evaluated token, with `crawler`, `purpose`
(`search`/`ai`), `operator`, `root_allowed`, `start_url_allowed`, `group`
(the matched group or `*`), `rule` (the deciding rule) and `explicit_group`.

## `sitemap`

`checked`, `found`, `complete` (false if limits were reached or a declared file
failed), `files[]` (`url`, `source`: `robots`/`index`/`default`, `guessed`,
`status_code`, `valid`, `kind`, `looks_like_html`, `url_count`, `error`,
`parse_error`, `invalid_urls[]`, `duplicate_urls[]`, `cross_host_urls[]`,
`children[]`), `url_count`, `urls[]` (first 1,000), `urls_truncated`,
`url_limit_reached`, `file_limit_reached`.

## Exit codes (CLI)

| Code | Meaning |
|---|---|
| 0 | An audit was produced (`crawl.state` complete or partial). |
| 1 | `crawl.state` is `failed`. Reports are still written. |
| 2 | Invalid URL or option. |
| 130 | Interrupted. |
