"""Web Visibility: deterministic website visibility auditing.

Phase 01 provides the technical foundation: a bounded same-origin crawler,
technical analyzers, an evidence-backed issue model, a coverage-aware diagnostic
score, and terminal / JSON / Markdown reports.
"""

__version__ = "0.2.0"

#: Version of the JSON report contract. The major number changes on incompatible
#: changes; consumers (such as the Agent Skill) must check it before reading a report.
REPORT_SCHEMA_VERSION = "2.1"

__all__ = ["REPORT_SCHEMA_VERSION", "__version__"]
