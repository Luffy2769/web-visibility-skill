"""Unit tests for the coverage-aware scoring arithmetic.

End-to-end score behaviour (real crawl -> analyzers -> score) is covered in
tests/integration/test_hardening.py.
"""

import pytest

from web_visibility.models import Category, Issue, Severity
from web_visibility.scoring import (
    CATEGORY_WEIGHTS,
    SEVERITY_WEIGHTS,
    Coverage,
    compute_score,
)


def issue(
    rule: str = "r",
    category: Category = Category.METADATA,
    severity: Severity = Severity.MEDIUM,
    urls: tuple[str, ...] = ("https://example.com/",),
    confidence: float = 1.0,
    prevalence: float | None = None,
) -> Issue:
    return Issue(
        id=rule,
        category=category,
        severity=severity,
        title="t",
        description="d",
        evidence="e",
        recommendation="r",
        confidence=confidence,
        affected_urls=urls,
        prevalence=prevalence,
    )


def full_coverage(**overrides: Coverage) -> dict[Category, Coverage]:
    coverage = {c: Coverage("scored", "pages", 10, 10, 0) for c in CATEGORY_WEIGHTS}
    for name, value in overrides.items():
        coverage[Category(name)] = value
    return coverage


def category(score, cat: Category):  # type: ignore[no-untyped-def]
    return next(c for c in score.categories if c.category is cat)


def test_weights_sum_to_100_and_info_is_free() -> None:
    assert sum(CATEGORY_WEIGHTS.values()) == 100
    assert SEVERITY_WEIGHTS[Severity.INFO] == 0


def test_no_issues_with_full_coverage_scores_100() -> None:
    score = compute_score([], full_coverage(), pages_audited=5)
    assert score.overall == 100
    assert score.status == "complete"


def test_nothing_checked_is_not_scored_not_perfect() -> None:
    nothing = {
        c: Coverage("not-scored", "pages", 0, 0, 0, "crawl failed") for c in CATEGORY_WEIGHTS
    }
    score = compute_score([], nothing, pages_audited=0)
    assert score.overall is None
    assert score.status == "not-scored"
    assert all(c.points is None for c in score.categories)


def test_unchecked_links_are_na_and_excluded_from_overall() -> None:
    coverage = full_coverage(links=Coverage("not-scored", "link targets", 30, 0, 0, "not checked"))
    score = compute_score([issue(category=Category.METADATA, urls=())], coverage, pages_audited=1)
    links = category(score, Category.LINKS)
    assert links.points is None  # N/A, never 20/20
    assert score.status == "partial"
    assert score.scored_weight == 80
    metadata_points = category(score, Category.METADATA).points
    expected = round(100 * (80 - CATEGORY_WEIGHTS[Category.METADATA] + metadata_points) / 80)
    assert score.overall == expected


def test_partial_coverage_is_scored_and_flagged() -> None:
    coverage = full_coverage(links=Coverage("partial", "link targets", 30, 10, 2, "20 unchecked"))
    score = compute_score([], coverage, pages_audited=3)
    assert category(score, Category.LINKS).points == 20
    assert score.status == "partial"
    assert score.scored_weight == 100


def test_penalty_scales_with_prevalence() -> None:
    weight = CATEGORY_WEIGHTS[Category.METADATA]
    one = compute_score([issue(urls=("u1",))], full_coverage(), pages_audited=4)
    every = compute_score([issue(urls=("u1", "u2", "u3", "u4"))], full_coverage(), pages_audited=4)
    medium = SEVERITY_WEIGHTS[Severity.MEDIUM]
    assert category(one, Category.METADATA).points == pytest.approx(
        weight * (1 - medium / 4), abs=0.1
    )
    assert category(every, Category.METADATA).points == pytest.approx(
        weight * (1 - medium), abs=0.1
    )


def test_many_failures_are_not_hidden_behind_one_rule_cap() -> None:
    # 19 broken targets out of 20 checked, each with prevalence 1/20.
    broken = [issue(rule="broken-internal-link", category=Category.LINKS, severity=Severity.HIGH,
                    prevalence=1 / 20, confidence=0.95) for _ in range(19)]  # fmt: skip
    one = compute_score(broken[:1], full_coverage(), pages_audited=1)
    many = compute_score(broken, full_coverage(), pages_audited=1)
    one_points = category(one, Category.LINKS).points
    many_points = category(many, Category.LINKS).points
    assert one_points is not None and many_points is not None
    assert many_points < one_points - 10  # dozens of failures cost proportionally more
    assert many_points == pytest.approx(20 * (1 - 0.75 * 0.95 * 19 / 20), abs=0.1)


def test_confidence_reduces_penalty() -> None:
    sure = compute_score([issue(confidence=1.0)], full_coverage(), pages_audited=1)
    unsure = compute_score([issue(confidence=0.35)], full_coverage(), pages_audited=1)
    assert category(unsure, Category.METADATA).points > category(sure, Category.METADATA).points


def test_info_issues_do_not_deduct() -> None:
    score = compute_score([issue(severity=Severity.INFO)], full_coverage(), pages_audited=1)
    assert score.overall == 100


def test_category_floor_is_zero() -> None:
    issues = [issue(rule=f"r{i}", severity=Severity.CRITICAL, urls=()) for i in range(3)]
    score = compute_score(issues, full_coverage(), pages_audited=1)
    assert category(score, Category.METADATA).points == 0
    assert score.overall == 100 - CATEGORY_WEIGHTS[Category.METADATA]


def test_deductions_are_reported_per_rule() -> None:
    issues = [issue(rule="a"), issue(rule="a"), issue(rule="b", severity=Severity.LOW)]
    deductions = category(compute_score(issues, full_coverage(), 1), Category.METADATA).deductions
    assert {d.rule_id: d.issues for d in deductions} == {"a": 2, "b": 1}


def test_score_is_deterministic() -> None:
    issues = [issue(rule="a"), issue(rule="b", category=Category.LINKS, severity=Severity.HIGH)]
    assert compute_score(issues, full_coverage(), 3) == compute_score(
        list(reversed(issues)), full_coverage(), 3
    )
