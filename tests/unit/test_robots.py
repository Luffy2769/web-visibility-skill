import pytest

from web_visibility.robots import RobotsTxt

SAMPLE = """
# Comment line
User-agent: *
Disallow: /private/
Allow: /private/public-page
Disallow: /*.pdf$
Disallow: /search?

User-agent: BadBot
User-agent: OtherBot
Disallow: /

User-agent: WebVisibilitySkill
Disallow: /no-audit/
Crawl-delay: 5

Sitemap: https://example.com/sitemap.xml
Sitemap: https://example.com/news-sitemap.xml
"""


@pytest.fixture
def robots() -> RobotsTxt:
    return RobotsTxt.parse(SAMPLE)


def test_groups_and_sitemaps_are_parsed(robots: RobotsTxt) -> None:
    assert len(robots.groups) == 3
    assert robots.groups[1].user_agents == ("badbot", "otherbot")
    assert robots.sitemaps == (
        "https://example.com/sitemap.xml",
        "https://example.com/news-sitemap.xml",
    )


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/", True),
        ("/about", True),
        ("/private/", False),
        ("/private/secret", False),
        ("/private/public-page", True),  # longer Allow wins
        ("/files/report.pdf", False),  # wildcard + end anchor
        ("/files/report.pdf?download=1", True),  # $ anchors the end
        ("/search?q=x", False),
        ("/search", True),
        ("/robots.txt", True),  # always allowed
    ],
)
def test_generic_group_matching(robots: RobotsTxt, path: str, allowed: bool) -> None:
    assert robots.is_allowed(path, "*") is allowed


def test_full_urls_are_matched_by_path_and_query(robots: RobotsTxt) -> None:
    assert not robots.is_allowed("https://example.com/private/x?y=1")
    assert robots.is_allowed("https://example.com/private/public-page")


def test_specific_user_agent_group_replaces_generic(robots: RobotsTxt) -> None:
    assert not robots.is_allowed("/", "BadBot")
    assert not robots.is_allowed("/no-audit/x", "WebVisibilitySkill")
    # The specific group replaces "*" entirely, so /private/ is allowed for it.
    assert robots.is_allowed("/private/secret", "WebVisibilitySkill")


def test_unknown_agent_falls_back_to_star(robots: RobotsTxt) -> None:
    assert not robots.is_allowed("/private/x", "SomeCrawler")


def test_tie_between_allow_and_disallow_prefers_allow() -> None:
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /page\nAllow: /page\n")
    assert robots.is_allowed("/page")


def test_empty_disallow_allows_everything() -> None:
    robots = RobotsTxt.parse("User-agent: *\nDisallow:\n")
    assert robots.is_allowed("/anything")
    assert not robots.has_rules


def test_disallows_everything() -> None:
    assert RobotsTxt.parse("User-agent: *\nDisallow: /\n").disallows_everything()
    assert not RobotsTxt.parse("User-agent: *\nDisallow: /\nAllow: /$\n").disallows_everything()
    assert not RobotsTxt.parse("User-agent: *\nDisallow: /admin\n").disallows_everything()


def test_rules_before_any_user_agent_are_ignored_and_garbage_counted() -> None:
    robots = RobotsTxt.parse("Disallow: /\nthis is not a directive\nUser-agent: *\nAllow: /\n")
    assert robots.is_allowed("/")
    assert robots.ignored_lines == 1


def test_empty_file_allows_everything() -> None:
    robots = RobotsTxt.parse("")
    assert robots.groups == ()
    assert robots.is_allowed("/x")


def test_verdict_reports_deciding_rule_and_group() -> None:
    robots = RobotsTxt.parse(
        "User-agent: Googlebot\nDisallow: /private\n\nUser-agent: *\nDisallow: /\n"
    )
    google = robots.verdict("/private/x", "Googlebot")
    assert (google.allowed, google.group, str(google.rule)) == (
        False,
        "googlebot",
        "Disallow: /private",
    )
    other = robots.verdict("/anything", "SomeBot")
    assert (other.allowed, other.group) == (False, "*")
    assert robots.names_agent("googlebot") and not robots.names_agent("bingbot")


def test_percent_encoding_is_normalized_for_rules_and_targets() -> None:
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /%7Ejoe/\nDisallow: /caf%c3%a9\n")
    assert not robots.is_allowed("/~joe/index.html")
    assert not robots.is_allowed("/%7ejoe/index.html")
    assert not robots.is_allowed("/caf%C3%A9")


def test_homepage_only_block_is_not_disallow_everything() -> None:
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /$\n")
    assert not robots.is_allowed("/")
    assert robots.is_allowed("/about")
    assert not robots.disallows_everything()


def test_crawl_delay_per_group() -> None:
    robots = RobotsTxt.parse(
        "User-agent: *\nCrawl-delay: 3\n\nUser-agent: fastbot\nCrawl-delay: 0.5\n"
    )
    assert robots.crawl_delay("WebVisibilitySkill") == 3.0
    assert robots.crawl_delay("fastbot") == 0.5
