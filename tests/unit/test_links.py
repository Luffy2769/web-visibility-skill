import pytest

from web_visibility.analyzers import links
from web_visibility.analyzers.base import TargetStatus
from web_visibility.models import DiscoverySource, Severity

from factories import BASE, by_id, good_head, html_doc, ids, link_check, make_context, make_page


def body_page(path: str, body: str, **kwargs):  # type: ignore[no-untyped-def]
    return make_page(path, html_doc(good_head(path), body), **kwargs)


def status(
    code: int | None, *, error_kind: str | None = None, redirected: bool = False
) -> TargetStatus:
    return TargetStatus(
        f"{BASE}/x", code, f"{BASE}/x", redirected, "err" if error_kind else None, error_kind
    )


@pytest.mark.parametrize(
    ("code", "broken", "severity"),
    [
        (404, True, Severity.HIGH),
        (410, True, Severity.HIGH),
        (400, True, Severity.MEDIUM),
        (500, True, Severity.MEDIUM),
        (503, True, Severity.MEDIUM),
        (401, False, Severity.INFO),
        (403, False, Severity.INFO),
        (429, False, Severity.INFO),
    ],
)
def test_status_classification(code: int, broken: bool, severity: Severity) -> None:
    verdict = links.classify_target(status(code))
    assert verdict is not None
    assert verdict.broken is broken
    assert verdict.severity is severity


def test_working_and_redirected_targets_are_not_broken() -> None:
    assert links.classify_target(status(200)) is None
    assert links.classify_target(status(200, redirected=True)) is None


def test_network_failures_are_unverified_not_broken() -> None:
    for kind in ("timeout", "connection", "dns", "ssl", "blocked"):
        verdict = links.classify_target(status(None, error_kind=kind))
        assert verdict is not None and not verdict.broken
    loop = links.classify_target(status(None, error_kind="redirect-loop"))
    assert loop is not None and loop.broken


def test_5xx_has_lower_confidence_than_404() -> None:
    five = links.classify_target(status(502))
    four = links.classify_target(status(404))
    assert five is not None and four is not None
    assert five.confidence < four.confidence


def test_broken_internal_link_lists_every_source_page() -> None:
    pages = [
        body_page("/", '<a href="/gone">Gone</a><a href="/a">A</a>'),
        body_page("/a", '<a href="/gone">Gone again</a><a href="/">Home</a>'),
        make_page("/gone", status=404),
    ]
    broken = by_id(links.analyze(make_context(pages)), "broken-internal-link")
    assert len(broken) == 1
    assert set(broken[0].affected_urls) == {f"{BASE}/", f"{BASE}/a"}
    assert broken[0].details["target"] == f"{BASE}/gone"
    assert broken[0].details["anchor_texts"] == ["Gone", "Gone again"]


def test_unverified_and_redirect_targets() -> None:
    pages = [body_page("/", '<a href="/admin">Admin</a><a href="/old">Old</a><a href="/a">A</a>')]
    ctx = make_context(
        pages,
        link_checks={
            f"{BASE}/admin": link_check(f"{BASE}/admin", 403),
            f"{BASE}/a": link_check(f"{BASE}/a", 200),
        },
    )
    issues = links.analyze(ctx)
    assert "unverified-link" in ids(issues)
    assert "broken-internal-link" not in ids(issues)

    redirect_pages = [
        body_page("/", '<a href="/old">Old</a>'),
        make_page("/new", html_doc(good_head("/new"), '<a href="/">x</a>'), redirect_from="/old"),
    ]
    assert "internal-link-redirect" in ids(links.analyze(make_context(redirect_pages)))


def test_external_broken_links_are_capped_at_low() -> None:
    pages = [body_page("/", '<a href="https://other.example/dead">Ref</a><a href="/a">A</a>')]
    ctx = make_context(
        pages,
        link_checks={"https://other.example/dead": link_check("https://other.example/dead", 404)},
    )
    broken = by_id(links.analyze(ctx), "broken-external-link")[0]
    assert broken.severity is Severity.LOW


def test_anchor_text_checks() -> None:
    pages = [
        body_page(
            "/",
            '<a href="/a"></a><a href="/b"><img src="i.png" alt=""></a>'
            '<a href="/c">Click here</a><a href="/d">Pricing plans</a>',
        )
    ]
    issues = links.analyze(make_context(pages))
    assert by_id(issues, "empty-anchor-text")[0].details["links"] == ["/a", "/b"]
    assert by_id(issues, "generic-anchor-text")[0].severity is Severity.INFO


def test_insecure_internal_links_on_https_site() -> None:
    pages = [body_page("/", '<a href="http://example.com/a">A</a><a href="/b">B</a>')]
    assert "insecure-internal-link" in ids(links.analyze(make_context(pages)))


def test_no_internal_outlinks() -> None:
    pages = [body_page("/", '<a href="https://other.example/">Elsewhere</a><a href="#top">Top</a>')]
    assert "no-internal-outlinks" in ids(links.analyze(make_context(pages)))


def test_potential_orphan_requires_sitemap_discovery_and_no_inlinks() -> None:
    pages = [
        body_page("/", '<a href="/linked">L</a>'),
        body_page("/linked", '<a href="/">H</a>', via=DiscoverySource.SITEMAP),
        body_page("/orphan", '<a href="/">H</a>', via=DiscoverySource.SITEMAP),
    ]
    orphans = by_id(links.analyze(make_context(pages)), "potential-orphan-page")
    assert [o.affected_urls for o in orphans] == [(f"{BASE}/orphan",)]
    assert "Potential orphan page based on the current crawl" in orphans[0].description


def test_orphan_confidence_drops_when_crawl_was_partial() -> None:
    pages = [
        body_page("/", "<p>x</p>"),
        body_page("/o", '<a href="/">H</a>', via=DiscoverySource.SITEMAP),
    ]
    full = by_id(links.analyze(make_context(pages)), "potential-orphan-page")[0]
    partial_ctx = make_context(pages, pending=(f"{BASE}/more",))
    partial = by_id(links.analyze(partial_ctx), "potential-orphan-page")[0]
    assert partial.confidence < full.confidence
    assert "page limit" in partial.description


def test_self_links_do_not_count_as_inbound_links() -> None:
    pages = [
        body_page("/", "<p>x</p>"),
        body_page("/o", '<a href="/o">Self</a>', via=DiscoverySource.SITEMAP),
    ]
    assert "potential-orphan-page" in ids(links.analyze(make_context(pages)))


def test_unchecked_links_note() -> None:
    ctx = make_context([body_page("/", '<a href="/a">A</a>')], unchecked=(f"{BASE}/a",))
    assert "links-not-checked" in ids(links.analyze(ctx))


def test_links_are_classified_against_the_effective_site_url() -> None:
    # Started at https://example.com/ but the site lives on https://www.example.com/.
    from dataclasses import replace

    from web_visibility.analyzers import AuditContext

    from factories import make_crawl

    www = "https://www.example.com"
    home = make_page("/", html_doc(good_head("/"), f'<a href="{www}/gone">Gone</a>'))
    home = replace(home, requested_url=f"{BASE}/", final_url=f"{www}/")
    crawl = replace(
        make_crawl([home], link_checks={f"{www}/gone": link_check(f"{www}/gone", 404)}),
        site_url=f"{www}/",
    )
    issues = links.analyze(AuditContext.build(crawl))
    assert "broken-internal-link" in ids(issues)
    assert "broken-external-link" not in ids(issues)
