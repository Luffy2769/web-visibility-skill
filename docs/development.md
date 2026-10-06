# Development

## Local setup

Requires Python 3.11 or newer.

```bash
git clone https://github.com/luffy2769/web-visibility-skill
cd web-visibility-skill
python -m venv .venv
source .venv/bin/activate        # Windows (PowerShell): .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

The `dev` extra installs `pytest`, `pytest-cov`, `ruff`, `mypy` and `pyyaml`.
PyYAML is only used to validate the SKILL.md frontmatter.

## Running the checks

```bash
pytest                      # all tests (unit + integration), about 5 seconds
pytest tests/unit           # unit tests only
pytest -m integration       # fixture-site tests only
pytest --cov                # with coverage
ruff check src tests        # lint
ruff format src tests       # format (CI runs --check)
mypy                        # strict type checking of src/
```

No test touches the public internet. Integration tests start a local HTTP
server on an ephemeral port.

## Running the CLI

```bash
web-visibility --help
web-visibility audit https://example.com
web-visibility audit https://example.com --max-pages 50 --output ./reports
web-visibility audit https://example.com --format json > audit.json
python -m web_visibility audit https://example.com     # equivalent
```

### Auditing the fixture site by hand

```bash
python tests/fixture_server.py
# Fixture site running at http://127.0.0.1:54321/
web-visibility audit http://127.0.0.1:54321/ --allow-private-network --show-info
```

`--allow-private-network` is required because localhost is blocked by default
(SSRF protection).

## Project layout

```text
src/web_visibility/
  cli.py           CLI (Typer)
  audit.py         pipeline entry point
  crawler.py       crawl orchestration
  fetch.py         HTTP (the only network code)
  safety.py        SSRF guard
  urls.py          normalization
  parsing.py       HTML extraction
  robots.py        robots.txt
  sitemap.py       sitemaps
  models.py        data models
  scoring.py       diagnostic score
  analyzers/       checks (one module per concern)
  reporters/       terminal / JSON / Markdown
tests/
  factories.py     builders for pages and contexts
  fixture_server.py
  fixtures/site/   intentionally broken website
  unit/  integration/
```

## Adding an analyzer

1. Create `src/web_visibility/analyzers/my_check.py`:

   ```python
   from web_visibility.analyzers.base import AuditContext
   from web_visibility.models import Category, Issue, Rule, Severity

   MY_RULE = Rule(
       "my-rule-id", Category.TECHNICAL, Severity.LOW, "Short title",
       "What the site owner should do.",
   )
   RULES = (MY_RULE,)

   def analyze(ctx: AuditContext) -> list[Issue]:
       issues = []
       for page in ctx.pages:
           if condition(page):
               issues.append(MY_RULE.issue(
                   description="What was observed.",
                   evidence="The concrete value that proves it.",
                   confidence=0.9,
                   affected_urls=(page.final_url,),
               ))
       return issues
   ```

2. Register the module in `_MODULES` in `analyzers/__init__.py`.
3. Document every rule ID in `.agents/skills/web-visibility/references/technical-seo.md`
   (enforced by `test_every_rule_is_documented_in_the_reference`).
4. Add tests (next section).

## Adding tests

- **Unit tests** build pages from HTML strings with `tests/factories.py`:

  ```python
  from factories import html_doc, good_head, make_context, make_page

  def test_my_rule() -> None:
      ctx = make_context([make_page("/", html_doc(good_head("/"), "<p>body</p>"))])
      assert [i.id for i in my_check.analyze(ctx)] == ["my-rule-id"]
  ```

  Always pair a "detects it" test with a "does not flag the legitimate case" test.
- **Crawler/fetch tests** use `httpx.MockTransport` (see `tests/unit/test_crawler.py`).
- **End-to-end regressions** use `tests/fake_site.py`: `FakeSite` routes (host-
  specific or path-only) serve raw streamed bodies, and `audit(site, ...)` runs
  the real pipeline with an injected sleeper, so no test waits in real time.
- **Integration**: add pages to `tests/fixtures/site/` (`{{BASE}}` is replaced
  with the server URL) and extend `EXPECTED` in `tests/integration/test_fixture_audit.py`.

## Building the package

```bash
pip install build
python -m build            # creates dist/*.whl and dist/*.tar.gz
pip install dist/web_visibility_skill-0.2.0-py3-none-any.whl
web-visibility --version
```

The version lives in `src/web_visibility/__init__.py` (`__version__`) and is read
by the build backend (hatchling). The report schema version is
`REPORT_SCHEMA_VERSION` in the same file. Keep `SKILL.md` metadata (`version`,
`requires-cli`, `report-schema`) and the pinned install tag in sync. Tests check this.

## Debugging

`web-visibility audit <url> --verbose` logs every request and fetch failure to stderr.
