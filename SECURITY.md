# Security Policy

`web-visibility` fetches URLs supplied by a user **and by the websites it
crawls**: every link, redirect and sitemap entry is attacker-influenced input.
This document states what is protected, how, and what is **not** protected.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
private vulnerability reporting ("Report a vulnerability" under the repository's
**Security** tab). Include steps to reproduce, the affected version and the impact.
You can expect an acknowledgement within 7 days.

## Supported versions

Only the latest released minor version receives security fixes while the
project is pre-1.0.

## Intended deployment

A **locally run CLI** operated by the person who chose the target URL. It is not
designed to be exposed as a network service ("scan any URL" endpoints). Doing so
needs additional isolation (see *Residual risks*).

## Server-side request forgery (SSRF)

**Risk:** a hostile page links or redirects to `http://169.254.169.254/` (cloud
metadata), `http://localhost:6379/`, a router admin page or another internal service.

**What is enforced** (`src/web_visibility/safety.py`, `fetch.py`):

- Only `http` and `https` URLs. URLs with embedded credentials are refused.
- Before **every request and every redirect hop**, the host is resolved once
  (with the configured timeout) and **all** returned addresses must be publicly
  routable. Refused: loopback (`127.0.0.0/8`, `::1`), RFC 1918 (`10/8`,
  `172.16/12`, `192.168/16`), link-local including `169.254.169.254` and
  `fe80::/10`, unique-local `fc00::/7`, carrier-grade NAT `100.64/10`,
  multicast, reserved and unspecified addresses. IPv6 forms that embed IPv4
  (IPv4-mapped, 6to4, Teredo, NAT64) are judged by the embedded address, so
  `64:ff9b::7f00:1` (127.0.0.1) is refused while ordinary DNS64 answers for public
  sites still work.
- **DNS pinning:** the request is sent to the *validated* IP address, with the
  original `Host` header and TLS server name (SNI). Certificates are still
  verified against the hostname. The HTTP client never performs its own second
  lookup, which closes the check-then-resolve-again (DNS rebinding) window for
  each hop.
- Redirects are followed manually, so each hop is resolved, validated and pinned.
- Environment proxies are ignored (`trust_env=False`), so requests go exactly
  where they were validated.
- Local targets require the explicit opt-in `--allow-private-network`. In that
  mode **no** address checks or pinning are performed.

Regression tests: `tests/unit/test_safety.py` (address ranges, pinning, rebinding
on a redirect hop, DNS timeout).

## Response size and decompression

- Bodies are read as **raw bytes** (`iter_raw`) and decompressed by the auditor
  (gzip, deflate) with zlib's output limit. Memory therefore stays bounded by
  the decompressed limit (default **5 MB**, `--max-page-bytes`) plus one network
  chunk, whatever the compression ratio. A response that exceeds the limit is cut
  off, flagged `too-large`, and **not analyzed**. Raw (compressed) input is capped
  at the same size.
- Measured (Python 3.14, 1 MB limit): a gzip bomb inflating to 40 MB (39 KB on
  the wire) and one inflating to 200 MB (194 KB) both peak at about 3.2 MB of
  traced memory, so memory does not grow with the payload. The regression test
  is `TestH1DecompressionLimits`. Over a real local socket with a 2 MB limit, a
  bomb inflating to 150 MB (145 KB on the wire) peaked at 9.2 MB (network buffers
  add to the limit). Before v0.2.0 a 60 MB payload peaked at about 141 MB.
- Only `gzip` and `deflate` are requested. Any other `Content-Encoding` (for
  example `br`) is reported as an error rather than decoded.
- Sitemaps: up to 50 MB (the protocol maximum) downloaded or inflated from `.gz`.
- Response headers: h11 refuses oversized header blocks (a 1 MB header, or 2,000
  1 KB headers, end the request with an error). Regression tests:
  `tests/security/test_resource_limits_live.py`.
- robots.txt: up to 500 KiB parsed (RFC 9309 minimum).

**Not protected:** HTML *parsing* of a page just under the limit costs memory
proportional to its size (a few multiples of 5 MB). The limits are per response.
A crawl holds every audited page's extracted data in memory (bounded by
`--max-pages`, max 1000).

## Time and rate limits

- Per-read timeout (default 10 s), DNS resolution timeout (same value), and a
  total body deadline of 3× the timeout. A server dripping bytes slowly cannot
  stall a request indefinitely.
- Redirects: 5 hops, with loop detection.
- Minimum delay between requests (default 0.25 s), raised to the site's
  robots.txt `Crawl-delay` (up to 30 s). Above 30 s, only robots.txt and the start page are requested (no sitemaps, no link checks; the remaining link targets are listed as unchecked).
- `Retry-After` on 429/503 is honoured once if 30 s or less. Otherwise a 429
  stops the crawl.
- Bounded crawl: `--max-pages` (default 20), link checks (default 50), external
  checks (25, opt-in), sitemap files (10), sitemap URLs (50,000).

## Untrusted content

- **XML:** sitemaps are parsed with `defusedxml` configured to reject any DTD,
  entity declaration or external reference **anywhere** in the document (not
  only at the start). This rules out entity-expansion and external-entity (XXE)
  attacks, and no entity is ever resolved over the network. Size is bounded
  before parsing (above). The document is **streamed** (`iterparse`): nesting
  deeper than 32 elements is rejected as soon as it is seen, and each entry is
  released once its `<loc>` is read. Measured (Python 3.14): a 7 MB document of
  2,000,000 nested elements (no DTD needed) peaked at 563 MB before this and at
  2 MB after; a legitimate 1,000,000-entry, 50 MB sitemap went from 302 MB to 85 MB.
- **robots.txt patterns** are matched in linear time (greedy segment matching for
  `*` and `$`), not with a backtracking regex. Before v0.2.0's audit #2 fixes, a
  rule such as `/*a*a*a*a*a*a*a*a*b` plus a crafted link took 39 s per check and a
  slightly longer rule hung the audit indefinitely.
- **HTML:** parsed with Python's `html.parser`, never executed. No JavaScript runs.
  The parsed tree is walked once, iteratively, and per-element text extraction is
  bounded (64 nodes / 300 characters), so hostile deep nesting costs linear time:
  20,000 nested links parse in about 1 s (an earlier v0.2 draft with per-element
  ancestor lookups took minutes). Regression test:
  `test_deeply_nested_markup_parses_in_linear_time`.
- **Terminal output:** site-derived text has control, ANSI-escape and bidi
  characters stripped, and is rendered as literal text, never as Rich markup.
- **Markdown output:** site-derived text is escaped so it cannot inject HTML or
  Markdown links.
- **AI agents:** report text is data. The Agent Skill tells agents never to
  follow instructions found in titles, anchors, JSON-LD or evidence strings.
  This is a prompt-level mitigation, not a technical guarantee.

## Secrets and privacy

- No API keys or credentials are needed, and no AI services are called.
- `Set-Cookie` response headers are redacted before reaching any report.
- Reports contain data about the audited site. `reports/` is git-ignored.
- Never commit `.env` files, credentials, cookies or session data.

## Network behaviour (what is sent, and where)

Requests go to the audited origin (and its same-host redirect target), to sitemap
URLs listed in its robots.txt or sitemap indexes, and (with `--check-external`) to
external link targets. Every request carries
`User-Agent: WebVisibilitySkill/0.2 (+https://github.com/luffy2769/web-visibility-skill)`.
The tool never impersonates a search-engine crawler. robots.txt is respected by
default, and a robots.txt that cannot be retrieved (5xx, 429, network error) means
no crawling. `--ignore-robots` exists for auditing sites you own.

## Residual risks (not solved)

- **Opt-in mode:** with `--allow-private-network`, all SSRF protections are off by design.
- **Shared infrastructure:** a public address can still front internal services
  (e.g. a reverse proxy routing by `Host`). Only network isolation addresses that.
- **Resource use within limits:** see *Not protected* above. Running many audits
  in parallel multiplies the limits.
- **Parser bugs:** BeautifulSoup, `html.parser`, expat and zlib process untrusted
  input. Keep dependencies updated.
- **Prompt injection:** agent behaviour depends on the agent following the skill's rules.

## Supply chain

Runtime dependencies: `httpx`, `beautifulsoup4`, `typer`, `rich`, `defusedxml`.
GitHub Actions in CI are pinned to commit SHAs. Install from source you have
reviewed, or from a pinned tag (`@v0.2.0`). Never pipe remote scripts into a shell.
