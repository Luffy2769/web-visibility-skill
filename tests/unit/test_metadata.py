from web_visibility.analyzers import metadata
from web_visibility.models import Issue, Page, Severity

from factories import BASE, by_id, good_head, html_doc, ids, link_check, make_context, make_page


def analyze(*pages: Page, **kwargs: object) -> list[Issue]:
    return metadata.analyze(make_context(pages, **kwargs))


def test_clean_page_has_no_metadata_issues() -> None:
    assert analyze(make_page("/", html_doc(good_head("/")))) == []


def test_missing_and_empty_title() -> None:
    issues = analyze(
        make_page("/", html_doc(good_head("/"))),
        make_page("/a", html_doc('<link rel="canonical" href="/a">')),
        make_page("/b", html_doc('<title> </title><link rel="canonical" href="/b">')),
    )
    assert by_id(issues, "missing-title")[0].affected_urls == (f"{BASE}/a",)
    assert by_id(issues, "empty-title")[0].affected_urls == (f"{BASE}/b",)
    assert by_id(issues, "missing-title")[0].severity is Severity.HIGH


def test_short_and_long_titles_are_low_severity_heuristics() -> None:
    issues = analyze(
        make_page("/", html_doc(good_head("/", title="Home"))),
        make_page("/x", html_doc(good_head("/x", title="x" * 90))),
    )
    assert {i.id for i in issues} == {"short-title", "long-title"}
    assert all(i.severity is Severity.LOW and i.confidence < 0.8 for i in issues)


def test_duplicate_titles_are_grouped_with_all_urls() -> None:
    pages = [
        make_page(p, html_doc(good_head(p, title="Same Title For All"))) for p in ("/", "/a", "/b")
    ]
    dup = by_id(analyze(*pages), "duplicate-title")
    assert len(dup) == 1
    assert set(dup[0].affected_urls) == {f"{BASE}/", f"{BASE}/a", f"{BASE}/b"}
    assert dup[0].details["value"] == "Same Title For All"


def test_duplicates_consolidated_by_one_canonical_are_not_reported() -> None:
    head = '<title>Product</title><meta name="description" content="d"><link rel="canonical" href="/p">'
    issues = analyze(make_page("/p", html_doc(head)), make_page("/p-print", html_doc(head)))
    assert "duplicate-title" not in ids(issues)
    assert "duplicate-meta-description" not in ids(issues)


def test_noindex_pages_are_excluded_from_duplicate_detection() -> None:
    noindex = '<meta name="robots" content="noindex">'
    issues = analyze(
        make_page("/", html_doc(good_head("/", title="Thanks for your order"))),
        make_page("/t", html_doc(noindex + good_head("/t", title="Thanks for your order"))),
    )
    assert "duplicate-title" not in ids(issues)


def test_meta_description_checks() -> None:
    canon = '<title>Some descriptive title</title><link rel="canonical" href="{}">'
    issues = analyze(
        make_page("/a", html_doc(canon.format("/a"))),
        make_page("/b", html_doc(canon.format("/b") + '<meta name="description" content="">')),
        make_page("/c", html_doc(canon.format("/c") + '<meta name="description" content="x">' * 2)),
    )
    found = {i.id: i.affected_urls for i in issues}
    assert found["missing-meta-description"] == (f"{BASE}/a",)
    assert found["empty-meta-description"] == (f"{BASE}/b",)
    assert found["multiple-meta-descriptions"] == (f"{BASE}/c",)


def test_missing_canonical_not_reported_for_noindex_pages() -> None:
    issues = analyze(
        make_page("/a", html_doc("<title>Some descriptive title</title>")),
        make_page(
            "/b",
            html_doc(
                '<title>Another descriptive title</title><meta name="robots" content="noindex">'
            ),
        ),
    )
    missing = by_id(issues, "missing-canonical")
    assert [i.affected_urls for i in missing] == [(f"{BASE}/a",)]


def test_conflicting_vs_repeated_canonicals() -> None:
    title = "<title>Some descriptive title</title>"
    issues = analyze(
        make_page(
            "/a",
            html_doc(title + '<link rel="canonical" href="/a"><link rel="canonical" href="/b">'),
        ),
        make_page("/c", html_doc(title + '<link rel="canonical" href="/c">' * 2)),
    )
    multiple = {i.affected_urls[0]: i.severity for i in by_id(issues, "multiple-canonicals")}
    assert multiple == {f"{BASE}/a": Severity.MEDIUM, f"{BASE}/c": Severity.LOW}


def test_malformed_canonical() -> None:
    title = "<title>Some descriptive title</title>"
    issues = analyze(
        make_page("/a", html_doc(title + '<link rel="canonical">')),
        make_page("/b", html_doc(title + '<link rel="canonical" href="javascript:void(0)">')),
    )
    assert len(by_id(issues, "malformed-canonical")) == 2


def test_cross_host_canonical_is_info_not_error() -> None:
    issues = analyze(
        make_page(
            "/", html_doc(good_head("/").replace(f"{BASE}/", "https://partner.example/article"))
        )
    )
    cross = by_id(issues, "canonical-cross-host")
    assert cross and cross[0].severity is Severity.INFO


def test_canonical_target_status() -> None:
    head = '<title>Some descriptive title</title><link rel="canonical" href="{}">'
    issues = analyze(
        make_page("/a", html_doc(head.format("/gone"))),
        make_page("/b", html_doc(head.format("/moved"))),
        link_checks={
            f"{BASE}/gone": link_check(f"{BASE}/gone", 404),
            f"{BASE}/moved": link_check(f"{BASE}/moved", 200),
        },
    )
    assert by_id(issues, "canonical-target-error")[0].affected_urls == (f"{BASE}/a",)
    assert "canonicalized-to-other-url" in ids(issues)


def test_canonical_to_redirecting_crawled_page() -> None:
    head = '<title>Some descriptive title</title><link rel="canonical" href="/old">'
    issues = analyze(
        make_page("/a", html_doc(head)),
        make_page("/new", html_doc(good_head("/new")), redirect_from="/old"),
    )
    assert "canonical-target-redirects" in ids(issues)


def test_noindex_with_canonical_elsewhere_conflict() -> None:
    head = '<title>Some descriptive title</title><meta name="robots" content="noindex"><link rel="canonical" href="/other">'
    assert "canonical-noindex-conflict" in ids(analyze(make_page("/a", html_doc(head))))
