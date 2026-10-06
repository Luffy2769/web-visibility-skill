import pytest

from web_visibility.urls import (
    has_non_html_extension,
    is_skipped_scheme,
    normalize_url,
    origin,
    same_host,
    same_origin,
    same_site,
)

BASE = "https://example.com/dir/page.html"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/a", "https://example.com/a"),
        ("HTTPS://Example.COM/a", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/a/./b/../c", "https://example.com/a/c"),
        ("https://example.com/%7euser", "https://example.com/~user"),
        ("https://example.com/a%2fb", "https://example.com/a%2Fb"),
        ("https://example.com/a b", "https://example.com/a%20b"),
        ("https://example.com/café", "https://example.com/caf%C3%A9"),
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
        ("https://[::1]:8080/x", "https://[::1]:8080/x"),
    ],
)
def test_normalize_absolute(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("other.html", "https://example.com/dir/other.html"),
        ("/root", "https://example.com/root"),
        ("../up", "https://example.com/up"),
        ("//cdn.example.org/x.js", "https://cdn.example.org/x.js"),
        ("?q=1", "https://example.com/dir/page.html?q=1"),
        ("", "https://example.com/dir/page.html"),
    ],
)
def test_normalize_relative(href: str, expected: str) -> None:
    assert normalize_url(href, BASE) == expected


def test_fragments_do_not_create_separate_urls() -> None:
    variants = ["/page", "/page#", "/page#section", "/page#other"]
    assert {normalize_url(v, BASE) for v in variants} == {"https://example.com/page"}


def test_trailing_slash_is_preserved() -> None:
    # /about and /about/ may be different resources; never merge them.
    assert normalize_url("https://example.com/about") != normalize_url("https://example.com/about/")


def test_scheme_is_not_upgraded_and_www_is_kept() -> None:
    assert normalize_url("http://example.com/") == "http://example.com/"
    assert normalize_url("https://www.example.com/") == "https://www.example.com/"


def test_tracking_parameters_are_removed_and_others_kept_in_order() -> None:
    url = "https://example.com/p?b=2&utm_source=x&a=1&gclid=abc&fbclid=z&UTM_medium=y"
    assert normalize_url(url) == "https://example.com/p?b=2&a=1"


def test_query_only_tracking_becomes_empty() -> None:
    assert normalize_url("https://example.com/p?utm_campaign=x") == "https://example.com/p"


@pytest.mark.parametrize(
    "raw",
    [
        "mailto:info@example.com",
        "tel:+491234",
        "javascript:void(0)",
        "data:text/html,hi",
        "ftp://example.com/file",
        "https://user:pass@example.com/",
        "https://example.com:99999/",
        "not a url",
        "",
    ],
)
def test_non_http_or_invalid_urls_are_rejected(raw: str) -> None:
    assert normalize_url(raw) is None


def test_skipped_schemes() -> None:
    assert is_skipped_scheme("  MAILTO:x@example.com")
    assert not is_skipped_scheme("/path")


def test_origin_and_host_comparisons() -> None:
    assert origin("https://example.com:8443/a?b") == "https://example.com:8443"
    assert same_origin("https://example.com/a", "https://example.com/b")
    assert not same_origin("https://example.com/", "http://example.com/")
    assert same_host("https://example.com/", "http://example.com/")
    assert not same_host("https://example.com/", "https://www.example.com/")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/file.pdf", True),
        ("https://example.com/img.JPG", True),
        ("https://example.com/page.html", False),
        ("https://example.com/page", False),
        ("https://example.com/v1.2/page", False),
    ],
)
def test_non_html_extension(url: str, expected: bool) -> None:
    assert has_non_html_extension(url) is expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("https://example.com/", "https://www.example.com/", True),
        ("http://example.com/", "https://example.com/", True),
        # Subdomain moves are not adopted: without the Public Suffix List,
        # user.github.io vs github.io is indistinguishable from shop.example.com.
        ("https://example.com/", "https://shop.example.com/", False),
        ("https://github.io/", "https://user.github.io/", False),
        ("https://user.github.io/", "https://other.github.io/", False),
        ("https://example.com/", "https://example.org/", False),
        ("https://example.com/", "https://notexample.com/", False),
    ],
)
def test_same_site(a: str, b: str, expected: bool) -> None:
    assert same_site(a, b) is expected
