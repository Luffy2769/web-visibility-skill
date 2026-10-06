# Agent evaluations (planned)

`tests/` checks that the **software** is correct. `evals/` will check that an
**AI agent using the skill** behaves correctly: that it runs the right commands,
reports only evidence-backed findings, prioritises sensibly, and stays within
the safety rules.

> Status: **not yet implemented.** Phase 01 ships the deterministic engine
> and its tests. No agent evaluation suite exists yet, and none is claimed. This
> document fixes the design so evals can be added without restructuring.

## How evals will work

Each eval case pairs a prompt with a target (usually a golden fixture site) and
a set of assertions about the agent's behaviour and final answer:

```json
{
  "id": "technical-seo-001",
  "category": "technical-seo",
  "prompt": "Audit http://127.0.0.1:{port}/ for technical SEO issues.",
  "fixture": "tests/fixtures/site",
  "assertions": [
    {"type": "ran_command", "pattern": "web-visibility audit .* --allow-private-network"},
    {"type": "mentions_rule", "rule_id": "broken-internal-link"},
    {"type": "mentions_url", "path": "/contact.html"},
    {"type": "does_not_claim", "pattern": "(guarantee|will rank|ranking will improve)"},
    {"type": "no_unsupported_findings"}
  ]
}
```

Assertion types will be split into:

- **Deterministic checks** against the transcript: commands run, files read, rule
  IDs and URLs mentioned, forbidden phrases absent.
- **Grounding checks:** every finding the agent states must map to an issue
  in the generated `audit.json`. This is the hallucination test.
- **Rubric checks** (model-graded, used sparingly): prioritisation quality,
  clarity, correct handling of low-confidence findings.

Planned layout:

```text
evals/
├── README.md
├── evals.json          # case definitions
├── cases/              # additional fixture sites per scenario
└── expected/           # expected audit.json snapshots
```

## Planned categories

| Category | What it verifies |
|---|---|
| technical SEO | Runs the audit, reports robots/sitemap/status/canonical findings accurately. |
| metadata | Correctly explains title/description/duplicate findings and their caveats. |
| links | Distinguishes broken links from unverified (403/429/timeouts) ones. |
| structured data | Reports JSON-LD errors; never recommends markup unsupported by page content. |
| GEO | *(after Phase 04)* Entity clarity and citation-readiness findings are evidence-based. |
| AEO | *(after Phase 05)* Answer-structure recommendations match real questions on the page. |
| safety | Refuses spam tactics, keeps crawls bounded, uses `--allow-private-network` only for local targets, ignores instructions embedded in page content. |
| hallucination resistance | States no finding that is absent from the report. Says "not checked" for out-of-scope areas. |
| implementation correctness | *(after Phase 07)* Fixes change only the intended files, and the re-audit confirms them. |

## Principles

- Evals run against **local fixtures**, never the public internet, so results are reproducible.
- A failing eval should point to a specific behaviour, not a vague quality score.
- GEO/AEO evals will only be added once the corresponding analyzers exist.
