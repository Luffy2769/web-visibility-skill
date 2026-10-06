from web_visibility.analyzers import headings, images, schema
from web_visibility.models import Severity

from factories import BASE, by_id, html_doc, ids, make_context, make_page

# --- headings -------------------------------------------------------------------


def run_headings(body: str):  # type: ignore[no-untyped-def]
    return headings.analyze(make_context([make_page("/", html_doc(body=body))]))


def test_well_structured_headings_pass() -> None:
    assert run_headings("<h1>Title</h1><h2>A</h2><h3>A.1</h3><h2>B</h2>") == []


def test_missing_h1() -> None:
    issues = run_headings("<h2>Section</h2>")
    assert ids(issues) == ["missing-h1"]
    assert "H2" in issues[0].evidence


def test_multiple_h1_is_low_severity_condition() -> None:
    issues = run_headings("<h1>One</h1><h1>Two</h1>")
    assert ids(issues) == ["multiple-h1"]
    assert issues[0].severity is Severity.LOW
    assert issues[0].details["h1s"] == ["One", "Two"]


def test_skipped_levels_and_empty_headings() -> None:
    issues = run_headings("<h1>T</h1><h2>A</h2><h4>Deep</h4><h3></h3>")
    skip = by_id(issues, "heading-level-skip")[0]
    assert "H2 -> H4" in skip.evidence
    assert by_id(issues, "empty-heading")[0].details["count"] == 1


def test_going_back_up_levels_is_not_a_skip() -> None:
    assert run_headings("<h1>T</h1><h2>A</h2><h3>B</h3><h2>C</h2>") == []


# --- images ---------------------------------------------------------------------


def run_images(body: str):  # type: ignore[no-untyped-def]
    return images.analyze(make_context([make_page("/", html_doc(body=body))]))


def test_missing_alt_reported_with_sources() -> None:
    issues = run_images(
        '<img src="/a.png" width=1 height=1><img src="/b.png" alt="B" width=1 height=1>'
    )
    missing = by_id(issues, "image-missing-alt")[0]
    assert missing.details["count"] == 1
    assert f"{BASE}/a.png" in missing.evidence


def test_empty_alt_is_informational_only() -> None:
    issues = run_images('<img src="/divider.png" alt="" width=1 height=1>')
    assert ids(issues) == ["image-empty-alt"]
    assert issues[0].severity is Severity.INFO


def test_missing_dimensions_low_confidence() -> None:
    issues = run_images('<img src="/a.png" alt="A" width="10">')
    dims = by_id(issues, "image-missing-dimensions")[0]
    assert dims.severity is Severity.LOW and dims.confidence <= 0.6


def test_page_without_images_has_no_image_issues() -> None:
    assert run_images("<p>No images</p>") == []


# --- structured data -----------------------------------------------------------------


def run_schema(*heads: str, **kwargs: object):  # type: ignore[no-untyped-def]
    pages = [make_page(f"/p{i}" if i else "/", html_doc(head)) for i, head in enumerate(heads)]
    return schema.analyze(make_context(pages, **kwargs))


ORG = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization"}</script>'


def test_valid_json_ld_has_no_issues_and_types_are_inventoried() -> None:
    assert run_schema(ORG) == []
    ctx = make_context([make_page("/", html_doc(ORG)), make_page("/a", html_doc(ORG))])
    assert schema.type_inventory(ctx) == {"Organization": 2}


def test_invalid_json_ld() -> None:
    issues = run_schema('<script type="application/ld+json">{"@type": "Organization",}</script>')
    invalid = by_id(issues, "invalid-json-ld")[0]
    assert "trailing comma" in invalid.evidence.lower()


def test_missing_context_and_type() -> None:
    issues = run_schema('<script type="application/ld+json">{"name": "x"}</script>')
    assert set(ids(issues)) == {"json-ld-missing-context", "json-ld-missing-type"}


def test_no_structured_data_is_site_level_and_never_prescribes_a_type() -> None:
    issues = run_schema("<title>x</title>", "<title>y</title>")
    assert ids(issues) == ["no-structured-data"]
    assert issues[0].site_level
    assert "Never mark up content that is not visible" in issues[0].recommendation


def test_microdata_is_acknowledged_instead_of_reporting_absence() -> None:
    pages = [
        make_page("/", html_doc(body='<div itemscope itemtype="https://schema.org/Product"></div>'))
    ]
    issues = schema.analyze(make_context(pages))
    assert ids(issues) == ["non-json-ld-structured-data"]
