# Contributing

Thanks for your interest in improving `web-visibility-skill`. The project's
priorities, in order: **correctness, evidence, safety, then features.** A check
that produces false positives is worse than no check.

## Getting set up

See [docs/development.md](docs/development.md) for the full setup. In short:

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

## Branching and commits

- Fork the repository (or create a branch if you have access) from `main`.
- Use a descriptive branch name: `fix/canonical-relative-urls`, `feat/hreflang-check`.
- Keep commits focused. Write messages that explain *why*, not only what.
- Rebase on `main` before opening a pull request.

## Before you open a pull request

All of these must pass, and CI runs them too:

```bash
ruff check src tests
ruff format --check src tests
mypy
pytest --cov
```

## Code quality expectations

- Type hints everywhere. `mypy --strict` must pass on `src/`.
- Small, single-purpose functions with clear names. Docstrings where intent isn't obvious.
- No network access in unit tests. Use `httpx.MockTransport`, as in
  `tests/unit/test_crawler.py`, or the local fixture server.
- No `print()` in library code. Use the reporters, or `logging` for diagnostics.
- Don't swallow exceptions silently. Network and parse failures become *data*
  (`FetchResult.error`, `Page.error`), and are reported as findings.
- New runtime dependencies need a clear justification in the PR description.

## Adding or changing a check

1. **Pick the analyzer** in `src/web_visibility/analyzers/`, or create a new module
   exposing `analyze(ctx) -> list[Issue]` and a `RULES` tuple. Register new
   modules in `analyzers/__init__.py`.
2. **Declare a `Rule`** with a stable kebab-case ID, category, default severity,
   title and recommendation. Rule IDs are part of the public JSON contract, so
   don't rename them casually.
3. **Produce evidence.** Every issue needs a concrete `evidence` string (what was
   observed, with values) and the affected URLs.
4. **Choose severity and confidence honestly.** Reserve `critical`/`high` for
   conditions with clear impact. Use `info` for things that are often
   intentional. Lower the confidence for heuristics.
5. **Phrase findings conservatively.** Describe the condition. Never promise ranking
   effects or claim certainty the crawl can't provide.
6. **Test both directions, through the real pipeline.** Add a unit test proving
   the issue is detected *and* one proving it is not raised where it shouldn't be
   (the false-positive guard). Then add an end-to-end test using
   `tests/fake_site.py` (controlled transport → crawler → analyzers → scoring):
   past bugs survived isolated unit tests. If the fixture site changes, update
   the golden set in `tests/integration/test_fixture_audit.py` deliberately.
7. **Mind coverage and confidence.** If a check can be inconclusive (transient
   errors, client-rendered pages, partial crawls), lower confidence or report
   coverage instead of asserting a defect.
8. **Document it** in `.agents/skills/web-visibility/references/technical-seo.md`.
   A test fails if any rule ID is undocumented.

## Documentation changes

- `SKILL.md` must stay concise (under 500 lines; a test enforces this). Put
  domain detail in `references/`.
- If you change the JSON report, update `docs/report-format.md` and bump
  `SCHEMA_VERSION` (minor for additive changes, major for breaking ones).
- Add a line to `CHANGELOG.md` under an "Unreleased" heading.

## Pull request expectations

- Describe the problem, the approach, and how you verified it.
- Include before/after output (terminal or JSON excerpt) for behaviour changes.
- Keep PRs reviewable. Split large changes.
- Be ready to discuss false-positive risk for any new check.

## Out of scope

Contributions that automate link building, submit content to third-party sites,
generate fake reviews or content, cloak, or otherwise game search engines will
not be accepted. See the project's ethics section in the README.

## Security issues

Please report vulnerabilities privately. See [SECURITY.md](SECURITY.md).
