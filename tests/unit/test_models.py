import pytest

from web_visibility.analyzers import all_rules
from web_visibility.models import Category, Issue, Rule, Severity

from factories import good_head, html_doc, make_page


def test_rule_creates_consistent_issues() -> None:
    rule = Rule("demo", Category.LINKS, Severity.LOW, "Demo", "Fix it.")
    issue = rule.issue(description="d", evidence="seen", confidence=0.9, affected_urls=["u"])
    assert (issue.id, issue.category, issue.severity, issue.title) == (
        "demo",
        Category.LINKS,
        Severity.LOW,
        "Demo",
    )
    assert issue.affected_url == "u"
    assert (
        rule.issue(description="d", evidence="e", confidence=1, severity=Severity.HIGH).severity
        is Severity.HIGH
    )


def test_issue_requires_evidence_and_valid_confidence() -> None:
    kwargs = {
        "id": "x",
        "category": Category.LINKS,
        "severity": Severity.LOW,
        "title": "t",
        "description": "d",
        "recommendation": "r",
    }
    with pytest.raises(ValueError, match="evidence"):
        Issue(evidence="  ", confidence=1.0, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="confidence"):
        Issue(evidence="e", confidence=1.5, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="prevalence"):
        Issue(evidence="e", confidence=1.0, prevalence=2.0, **kwargs)  # type: ignore[arg-type]


def test_issue_and_page_are_immutable() -> None:
    page = make_page("/", html_doc(good_head("/")))
    with pytest.raises(AttributeError):
        page.status_code = 500  # type: ignore[misc]


def test_page_accessors() -> None:
    page = make_page(
        "/",
        html_doc(
            good_head("/"),
            "<h1>A</h1><h2>B</h2><a href='/x'>x</a><a href='https://o.example/'>o</a>",
        ),
    )
    assert page.title == "A descriptive page title for /"
    assert page.h1s == ("A",)
    assert len(page.internal_links) == 1 and len(page.external_links) == 1
    assert page.is_html and page.is_success


def test_severity_ordering() -> None:
    assert sorted(Severity, key=lambda s: s.rank)[0] is Severity.CRITICAL
    assert Severity.INFO.rank == 4


def test_rule_ids_are_unique_and_kebab_case() -> None:
    rules = all_rules()
    rule_ids = [r.id for r in rules]
    assert len(rule_ids) == len(set(rule_ids))
    assert all(r.islower() and " " not in r and "_" not in r for r in rule_ids)


def test_every_rule_is_documented_in_the_reference() -> None:
    from pathlib import Path

    reference = (
        Path(__file__).parents[2] / ".agents/skills/web-visibility/references/technical-seo.md"
    )
    text = reference.read_text(encoding="utf-8")
    missing = [r.id for r in all_rules() if f"`{r.id}`" not in text]
    assert not missing, f"rules missing from technical-seo.md: {missing}"
