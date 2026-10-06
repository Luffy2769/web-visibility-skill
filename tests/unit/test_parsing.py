from web_visibility.parsing import parse_html, parse_json_ld

URL = "https://example.com/blog/post.html"


def parse(head: str = "", body: str = "", url: str = URL):  # type: ignore[no-untyped-def]
    return parse_html(f"<html><head>{head}</head><body>{body}</body></html>".encode(), url)


# --- metadata ---------------------------------------------------------------


def test_title_whitespace_is_collapsed() -> None:
    assert parse("<title>\n  Hello \t world </title>").titles == ("Hello world",)


def test_svg_title_is_not_the_document_title() -> None:
    content = parse(body="<svg><title>Close icon</title></svg>")
    assert content.titles == ()


def test_multiple_titles_are_all_recorded() -> None:
    assert parse("<title>A</title><title>B</title>").titles == ("A", "B")


def test_meta_description_name_is_case_insensitive() -> None:
    content = parse('<meta name="Description" content=" A summary ">')
    assert content.meta_descriptions == ("A summary",)


def test_meta_description_without_content_is_empty_string() -> None:
    assert parse('<meta name="description">').meta_descriptions == ("",)


def test_canonicals_raw_values_and_missing_href() -> None:
    content = parse('<link rel="canonical" href="/x"><link rel="Canonical">')
    assert content.canonicals == ("/x", None)


def test_meta_robots() -> None:
    assert parse('<meta name="robots" content="noindex, nofollow">').meta_robots == (
        "noindex, nofollow",
    )


# --- headings -----------------------------------------------------------------


def test_headings_in_document_order() -> None:
    content = parse(body="<h2>B</h2><h1> A <span>x</span></h1><h3></h3>")
    assert [(h.level, h.text) for h in content.headings] == [(2, "B"), (1, "A x"), (3, "")]


# --- links ----------------------------------------------------------------------


def test_link_classification_and_resolution() -> None:
    content = parse(
        body=(
            '<a href="/a">A</a>'
            '<a href="b.html#frag">B</a>'
            '<a href="https://other.org/x" rel="nofollow noopener">Other</a>'
            '<a href="http://example.com/insecure">Insecure</a>'
            '<a href="mailto:x@example.com">Mail</a>'
            '<a href="javascript:void(0)">JS</a>'
            "<a>No href</a>"
        )
    )
    links = {link.url: link for link in content.links}
    assert set(links) == {
        "https://example.com/a",
        "https://example.com/blog/b.html",
        "https://other.org/x",
        "http://example.com/insecure",
    }
    assert links["https://example.com/a"].internal
    assert links["http://example.com/insecure"].internal  # same host, different scheme
    assert not links["https://other.org/x"].internal
    assert links["https://other.org/x"].nofollow


def test_base_href_is_respected() -> None:
    content = parse('<base href="https://example.com/docs/">', '<a href="intro">Intro</a>')
    assert content.links[0].url == "https://example.com/docs/intro"


def test_link_accessible_name_fallbacks() -> None:
    content = parse(
        body=(
            '<a href="/1" aria-label="Open menu"></a>'
            '<a href="/2" title="Profile"></a>'
            '<a href="/3"><img src="logo.png" alt="Home"></a>'
            '<a href="/4"><img src="x.png" alt=""></a>'
        )
    )
    assert [link.text for link in content.links] == ["Open menu", "Profile", "Home", ""]


# --- images ---------------------------------------------------------------------


def test_missing_alt_is_distinct_from_empty_alt() -> None:
    content = parse(
        body='<img src="a.png"><img src="b.png" alt=""><img src="c.png" alt="Chart" width=1 height=2>'
    )
    alts = [img.alt for img in content.images]
    assert alts == [None, "", "Chart"]
    assert content.images[0].src == "https://example.com/blog/a.png"
    assert content.images[2].width == "1"


def test_noscript_fallback_images_are_ignored() -> None:
    content = parse(body='<img src="lazy.png" alt="x"><noscript><img src="lazy.png"></noscript>')
    assert len(content.images) == 1


def test_image_inside_link_is_flagged() -> None:
    content = parse(body='<a href="/"><img src="a.png" alt="Home"></a><img src="b.png" alt="B">')
    assert [img.in_link for img in content.images] == [True, False]


# --- structured data ---------------------------------------------------------------


def test_json_ld_types_and_graph() -> None:
    content = parse(
        '<script type="application/ld+json">'
        '{"@context": "https://schema.org", "@graph": ['
        '{"@type": "Organization"}, {"@type": ["WebSite", "Thing"]}]}'
        "</script>"
    )
    block = content.json_ld[0]
    assert block.valid
    assert block.types == ("Organization", "WebSite", "Thing")
    assert not block.missing_context


def test_json_ld_type_attribute_variants() -> None:
    content = parse(
        '<script type="Application/LD+JSON; charset=utf-8">{"@type": "Person"}</script>'
    )
    assert content.json_ld[0].types == ("Person",)
    assert content.json_ld[0].missing_context


def test_invalid_json_ld_is_recorded_not_raised() -> None:
    block = parse_json_ld('{"@type": "Organization",}')
    assert not block.valid
    assert block.error is not None and "invalid JSON" in block.error


def test_json_ld_edge_cases() -> None:
    assert parse_json_ld("   ").error == "empty JSON-LD block"
    assert parse_json_ld("[1, 2]").error == "JSON-LD contains no JSON objects"
    assert parse_json_ld('{"@context": "https://schema.org"}').missing_type


def test_microdata_and_rdfa_detection() -> None:
    content = parse(body='<div itemscope itemtype="https://schema.org/Person"></div>')
    assert content.has_microdata and not content.has_rdfa


# --- subresources and malformed input -------------------------------------------------


def test_subresources() -> None:
    content = parse(
        '<link rel="stylesheet" href="http://cdn.example.org/s.css"><script src="/app.js"></script>',
        '<img src="i.png"><iframe src="https://video.example/embed"></iframe>',
    )
    assert set(content.subresources) == {
        "http://cdn.example.org/s.css",
        "https://example.com/app.js",
        "https://example.com/blog/i.png",
        "https://video.example/embed",
    }


def test_malformed_html_does_not_raise() -> None:
    content = parse_html(b"<html><head><title>Broken<body><h1>Unclosed <a href='/x'>link", URL)
    assert content.titles  # something sensible is still extracted


def test_lang_and_encoding() -> None:
    body = '<html lang="de"><head><title>Über uns</title></head></html>'.encode("latin-1")
    content = parse_html(body, URL, encoding="latin-1")
    assert content.lang == "de"
    assert content.titles == ("Über uns",)


# --- hardening: placement, crawler-specific robots meta, rendering signals ---------


def test_canonical_placement_is_recorded() -> None:
    content = parse_html(
        b"<html><head><link rel='canonical' href='/a'><img src='x.png'>"
        b"<link rel='canonical' href='/b'></head><body><link rel='canonical' href='/c'></body></html>",
        URL,
    )
    assert [(c.href, c.source) for c in content.canonical_links] == [
        ("/a", "head"),
        ("/b", "head-implicitly-closed"),
        ("/c", "body"),
    ]
    assert content.head_anomalies == ("<img>",)


def test_robots_meta_for_specific_crawlers() -> None:
    content = parse(
        '<meta name="robots" content="index"><meta name="GoogleBot" content="noindex">'
        '<meta name="otherbot" content="noindex">'
    )
    assert [(m.name, m.content) for m in content.robots_meta] == [
        ("robots", "index"),
        ("googlebot", "noindex"),
    ]


def test_visible_text_excludes_scripts_and_templates() -> None:
    content = parse(
        "<script>var a = 'lots of script text here';</script>",
        "<template><p>hidden</p></template><p>Hello world</p><!-- a comment -->",
    )
    assert content.visible_text_length == len("Hello world")


def test_content_rich_page_with_scripts_is_not_a_shell() -> None:
    body = "<div id='root'><h1>Server rendered</h1>" + "<p>Real paragraph text.</p>" * 20 + "</div>"
    content = parse("<script src='/app.js'></script>", body)
    assert not content.likely_client_rendered
