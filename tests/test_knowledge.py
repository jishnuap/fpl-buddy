"""Article harvesting: config, discovery, extraction, storage, id resolution.

Nothing here touches the network. Feeds, sitemaps and article pages are served
from strings through respx, which is enough to exercise the parts that actually
broke in practice: a crawl that expands without bound, a paywalled page that
looks like a complete article, and a surname that matches the wrong player.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from fpl_buddy.knowledge.discover import discover
from fpl_buddy.knowledge.extract import extract
from fpl_buddy.knowledge.fetch import Fetcher
from fpl_buddy.knowledge.sources import Source, load_sources
from fpl_buddy.knowledge.store import (
    ArticleNote,
    KnowledgeStore,
    content_hash,
    make_id,
)
from fpl_buddy.knowledge.summarize import ArticleSummary, resolve_players

from .conftest import FIXTURE_DIR

HOST = "https://news.example.test"


def article_html(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><title>{title}</title></head><body>
    <nav><a href="/tag/fpl">tags</a><a href="/author/bob">bob</a></nav>
    <article><h1>{title}</h1><p>{body}</p></article></body></html>"""


def feed_xml(items: list[tuple[str, str, str]]) -> str:
    entries = "".join(
        f"<item><title>{t}</title><link>{u}</link><pubDate>{d}</pubDate></item>"
        for t, u, d in items
    )
    return f"<?xml version='1.0'?><rss><channel>{entries}</channel></rss>"


def make_source(**overrides) -> Source:
    payload = {
        "name": "example",
        "base_url": HOST,
        "discovery": {
            "feeds": [f"{HOST}/feed"],
            "include_patterns": [r"/20\d{2}/\d{2}/\d{2}/"],
            "exclude_patterns": ["/tag/", "/author/"],
        },
        "tags": ["tips"],
        "trust": "high",
        "request_delay_seconds": 0,
    }
    payload.update(overrides)
    return Source.model_validate(payload)


@pytest.fixture
def store(tmp_path: Path) -> KnowledgeStore:
    return KnowledgeStore(tmp_path / "knowledge")


@pytest.fixture
def fetcher(settings) -> Fetcher:
    return Fetcher(settings)


@pytest.fixture
def allow_robots():
    """robots.txt that permits everything, for every host in a test."""
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r".*/robots\.txt").respond(200, text="User-agent: *\nDisallow:")
        yield router


# ------------------------------------------------------------------------ config


def test_a_missing_source_file_is_not_an_error(tmp_path):
    """Harvesting is optional: no config must behave as if the feature is off."""
    assert load_sources(tmp_path / "nope.yaml").sources == []
    assert load_sources(None).sources == []


def test_sources_load_from_yaml(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        """
sources:
  - name: example
    base_url: https://news.example.test
    discovery:
      feeds: [https://news.example.test/feed]
      include_patterns: ['/20\\d{2}/']
    tags: [tips]
    ttl_days: 7
"""
    )
    config = load_sources(path)
    assert len(config.active) == 1
    assert config.sources[0].ttl_days == 7
    assert config.sources[0].discovery.include_patterns == [r"/20\d{2}/"]


def test_a_disabled_source_is_not_active(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        "sources:\n  - name: retired-source\n    base_url: https://x.test\n    enabled: false\n"
    )
    config = load_sources(path)
    assert len(config.sources) == 1
    assert config.active == []


def test_a_bad_regex_is_rejected_at_load_time(tmp_path):
    """Better to fail on startup than to skip every article silently."""
    path = tmp_path / "s.yaml"
    path.write_text(
        "sources:\n  - name: x\n    base_url: https://x.test\n"
        "    discovery:\n      include_patterns: ['[unclosed']\n"
    )
    with pytest.raises(ValueError, match="bad regex"):
        load_sources(path)


def test_duplicate_source_names_are_rejected(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        "sources:\n  - name: dup\n    base_url: https://a.test\n"
        "  - name: dup\n    base_url: https://b.test\n"
    )
    with pytest.raises(ValueError, match="Duplicate source names"):
        load_sources(path)


def test_a_name_must_be_a_slug():
    with pytest.raises(ValueError, match="lowercase slug"):
        Source.model_validate({"name": "Not A Slug", "base_url": HOST})


def test_credentials_come_from_the_environment_not_the_file(monkeypatch):
    source = make_source(cookie_env="TEST_COOKIE")
    assert source.cookie() is None
    monkeypatch.setenv("TEST_COOKIE", "session=abc")
    assert source.cookie() == "session=abc"


def test_a_missing_credential_warns_once_not_per_request(monkeypatch, caplog):
    """One line per run, not one per fetch."""
    monkeypatch.delenv("TEST_COOKIE", raising=False)
    source = make_source(cookie_env="TEST_COOKIE")
    for _ in range(5):
        source.cookie()
    assert caplog.text.count("references TEST_COOKIE") == 1


# --------------------------------------------------------------------- discovery


def test_a_feed_yields_dated_articles(settings, fetcher, allow_robots):
    allow_robots.get(f"{HOST}/feed").respond(
        200,
        text=feed_xml(
            [
                ("New", f"{HOST}/2026/07/25/new-article", "Sat, 25 Jul 2026 08:00:00 +0000"),
                ("Old", f"{HOST}/2026/07/20/old-article", "Mon, 20 Jul 2026 08:00:00 +0000"),
            ]
        ),
    )
    found = discover(make_source(), fetcher)
    assert [c.url for c in found] == [
        f"{HOST}/2026/07/25/new-article",
        f"{HOST}/2026/07/20/old-article",
    ], "newest first"
    assert found[0].published.year == 2026


def test_discovery_rejects_other_hosts_and_non_articles(settings, fetcher, allow_robots):
    allow_robots.get(f"{HOST}/feed").respond(
        200,
        text=feed_xml(
            [
                ("Good", f"{HOST}/2026/07/25/ok", "Sat, 25 Jul 2026 08:00:00 +0000"),
                ("Offsite", "https://evil.test/2026/07/25/nope", "Sat, 25 Jul 2026 08:00:00 +0000"),
                ("Tag page", f"{HOST}/tag/fpl", "Sat, 25 Jul 2026 08:00:00 +0000"),
                ("Homepage", f"{HOST}/", "Sat, 25 Jul 2026 08:00:00 +0000"),
            ]
        ),
    )
    urls = [c.url for c in discover(make_source(), fetcher)]
    assert urls == [f"{HOST}/2026/07/25/ok"]


def test_root_crawling_finds_articles_when_there_is_no_feed(settings, fetcher, allow_robots):
    allow_robots.get(f"{HOST}/articles").respond(
        200,
        text="""<html><body>
        <a href="/2026/07/25/one">one</a>
        <a href="/2026/07/24/two">two</a>
        <a href="/tag/fpl">excluded</a>
        <a href="https://evil.test/2026/07/25/x">offsite</a>
        </body></html>""",
    )
    source = make_source(
        discovery={
            "roots": [f"{HOST}/articles"],
            "include_patterns": [r"/20\d{2}/\d{2}/\d{2}/"],
            "exclude_patterns": ["/tag/"],
        }
    )
    urls = {c.url for c in discover(source, fetcher)}
    assert urls == {f"{HOST}/2026/07/25/one", f"{HOST}/2026/07/24/two"}


def test_crawling_does_not_follow_links_off_the_root_by_default(settings, fetcher, allow_robots):
    """The bug this pins: max_depth defaulting to 1 turned four listing pages
    into hundreds of requests by following every nav link it saw."""
    root = allow_robots.get(f"{HOST}/articles").respond(
        200, text='<a href="/section/news">news</a><a href="/2026/07/25/one">one</a>'
    )
    section = allow_robots.get(f"{HOST}/section/news").respond(200, text="<a href='/x'>x</a>")

    source = make_source(
        discovery={"roots": [f"{HOST}/articles"], "include_patterns": [r"/20\d{2}/"]}
    )
    discover(source, fetcher)

    assert root.called
    assert not section.called, "depth 0 must not leave the roots"


def test_the_page_budget_stops_a_runaway_crawl(settings, fetcher, allow_robots):
    allow_robots.get(url__regex=rf"{HOST}/section/\d+").respond(200, text="<a href='/y'>y</a>")
    allow_robots.get(f"{HOST}/articles").respond(
        200, text="".join(f'<a href="/section/{i}">s</a>' for i in range(50))
    )
    source = make_source(
        discovery={
            "roots": [f"{HOST}/articles"],
            "include_patterns": [r"/20\d{2}/"],
            "max_depth": 1,
            "max_pages_per_run": 4,
        }
    )
    discover(source, fetcher)
    fetched = [c for c in allow_robots.calls if "robots" not in str(c.request.url)]
    assert len(fetched) <= 4


def test_a_full_feed_skips_the_root_crawl_entirely(settings, fetcher, allow_robots):
    """Politeness: if the feed already gave us a full run, don't crawl at all."""
    allow_robots.get(f"{HOST}/feed").respond(
        200,
        text=feed_xml(
            [
                (f"a{i}", f"{HOST}/2026/07/{i:02d}/post", f"Sat, {i:02d} Jul 2026 08:00:00 +0000")
                for i in range(10, 15)
            ]
        ),
    )
    root = allow_robots.get(f"{HOST}/articles").respond(200, text="")
    source = make_source(
        discovery={
            "feeds": [f"{HOST}/feed"],
            "roots": [f"{HOST}/articles"],
            "include_patterns": [r"/20\d{2}/"],
            "max_articles_per_run": 3,
        }
    )
    found = discover(source, fetcher)
    assert len(found) == 3
    assert not root.called


def test_tracking_parameters_are_stripped_but_real_ones_kept(settings, fetcher, allow_robots):
    """The BBC's feed appends ?at_medium=RSS. Left in, the same article looks new
    the day a publisher changes its analytics."""
    allow_robots.get(f"{HOST}/feed").respond(
        200,
        text=feed_xml(
            [
                (
                    "Tracked",
                    # &amp; is how a feed actually escapes this. Parsed without
                    # unescaping, the second parameter is named "amp;at_campaign"
                    # and slips past the tracking filter.
                    f"{HOST}/2026/07/25/one?at_medium=RSS&amp;at_campaign=rss&amp;utm_source=x",
                    "Sat, 25 Jul 2026 08:00:00 +0000",
                ),
                ("Real query", f"{HOST}/2026/07/24/two?p=1234", "Fri, 24 Jul 2026 08:00:00 +0000"),
            ]
        ),
    )
    urls = [c.url for c in discover(make_source(), fetcher)]
    assert urls[0] == f"{HOST}/2026/07/25/one", "campaign parameters removed"
    assert urls[1] == f"{HOST}/2026/07/24/two?p=1234", "genuine identifiers kept"


def test_a_dead_feed_does_not_raise(settings, fetcher, allow_robots):
    allow_robots.get(f"{HOST}/feed").respond(500, text="boom")
    assert discover(make_source(), fetcher) == []


# ------------------------------------------------------------------------ robots


def test_robots_disallow_is_honoured(settings):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r".*/robots\.txt").respond(
            200, text="User-agent: *\nDisallow: /private/"
        )
        fetcher = Fetcher(settings)
        assert fetcher.allowed(f"{HOST}/2026/07/25/ok") is True
        assert fetcher.allowed(f"{HOST}/private/thing") is False


def test_a_missing_robots_file_means_allowed(settings):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r".*/robots\.txt").respond(404)
        assert Fetcher(settings).allowed(f"{HOST}/anything") is True


def test_a_disallowed_url_is_never_fetched(settings):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r".*/robots\.txt").respond(
            200, text="User-agent: *\nDisallow: /private/"
        )
        page = router.get(f"{HOST}/private/x").respond(200, text="secret")
        result = Fetcher(settings).get(f"{HOST}/private/x")
        assert not page.called
        assert not result.ok


def test_conditional_requests_send_the_etag(settings, allow_robots):
    route = allow_robots.get(f"{HOST}/a").respond(304)
    result = Fetcher(settings).get(f"{HOST}/a", etag='"abc"')
    assert result.unchanged
    assert route.calls[0].request.headers["if-none-match"] == '"abc"'


def test_the_configured_cookie_is_sent(settings, allow_robots, monkeypatch):
    monkeypatch.setenv("TEST_COOKIE", "session=xyz")
    route = allow_robots.get(f"{HOST}/a").respond(200, text="hi")
    Fetcher(settings).get(f"{HOST}/a", source=make_source(cookie_env="TEST_COOKIE"))
    assert route.calls[0].request.headers["cookie"] == "session=xyz"


def test_a_network_error_is_reported_not_raised(settings, allow_robots):
    allow_robots.get(f"{HOST}/a").mock(side_effect=httpx.ConnectError("down"))
    result = Fetcher(settings).get(f"{HOST}/a")
    assert result.status == 0 and not result.ok


# --------------------------------------------------------------------- extraction


def test_extraction_pulls_the_article_and_drops_the_chrome():
    body = "Haaland is the obvious captain this week. " * 20
    article = extract(article_html("Captain picks", body), f"{HOST}/2026/07/25/x")
    assert article is not None
    assert "Haaland is the obvious captain" in article.text
    assert "tags" not in article.text and "bob" not in article.text
    assert article.access == "full"
    assert article.usable


def test_a_paywalled_article_is_marked_partial_not_treated_as_complete():
    """The failure this prevents: summarising an intro as if it were analysis."""
    body = (
        "Here are the best value picks for the new season. " * 15
        + " DEFENDERS This content is restricted to Chief Scout Members."
    )
    article = extract(article_html("Best value picks", body), f"{HOST}/2026/07/25/x")
    assert article is not None
    assert article.access == "partial"
    assert article.paywall_marker == "restricted to"


def test_empty_html_returns_nothing():
    assert extract("", f"{HOST}/x") is None
    assert extract("   ", f"{HOST}/x") is None


def test_a_navigation_only_page_is_not_usable():
    """Extraction may still return something; `usable` is what gates storage."""
    article = extract("<html><body><nav>menu</nav></body></html>", f"{HOST}/x")
    assert article is None or not article.usable


def test_a_stub_article_is_not_usable():
    article = extract(article_html("Tiny", "Too short."), f"{HOST}/2026/07/25/x")
    assert article is None or not article.usable


# ----------------------------------------------------------------------- titles
#
# The headline is what the agent reads in the article index when it picks which
# piece to open, so a wrong one is not cosmetic: twelve articles filed under
# "Join Our Leagues" are twelve articles it cannot tell apart.

FFS_URL = (
    "https://www.fantasyfootballscout.co.uk/2026/07/24"
    "/best-4-0m-defenders-for-fpl-2026-27-all-46-assessed"
)


def test_the_headline_wins_over_a_promo_widget_in_rendered_main_content():
    """The regression: every Fantasy Football Scout article stored as "Join Our
    Leagues".

    Firecrawl's `only_main_content` HTML arrives with the `<head>` stripped --
    no og:title, no JSON-LD, no usable `<title>` -- and trafilatura's
    metadata.title then falls back to the first heading in the fragment, which
    on this site is a mini-league promo sitting above the article.
    """
    html = (FIXTURE_DIR / "fantasy-football-scout-article.html").read_text(encoding="utf-8")

    article = extract(html, FFS_URL)

    assert article is not None
    assert article.title == "Best £4.0m defenders for FPL 2026/27: All 46 assessed"
    assert article.usable


def test_an_svg_title_is_not_mistaken_for_the_pages_title():
    """The `<title>` fallback used to match anywhere in the document, and the
    only one left in that stripped fragment belongs to an inline icon."""
    html = (FIXTURE_DIR / "fantasy-football-scout-article.html").read_text(encoding="utf-8")

    article = extract(html, FFS_URL)

    assert article is not None and article.title != "mobile"


def test_the_publishers_own_og_title_beats_a_heading():
    body = "Haaland is the obvious captain this week. " * 20
    html = f"""<!doctype html><html><head><title>Site</title>
    <meta property="og:title" content="Five things we learned"></head><body>
    <h1>Latest news</h1><article><h2>Sidebar heading</h2><p>{body}</p></article>
    </body></html>"""

    article = extract(html, f"{HOST}/2026/07/25/x")

    assert article is not None and article.title == "Five things we learned"


def test_a_json_ld_headline_is_read_from_the_graph():
    """Publishers nest the article node alongside ones for the site and the
    author, so the site's name is right there to be picked by mistake."""
    body = "Haaland is the obvious captain this week. " * 20
    graph = (
        '{"@context":"https://schema.org","@graph":['
        '{"@type":"WebSite","name":"News Example"},'
        '{"@type":"Article","headline":"Nine first impressions of the prices"}]}'
    )
    html = f"""<!doctype html><html><head><title>News Example</title>
    <script type="application/ld+json">{graph}</script></head><body>
    <div>Join Our Leagues</div><article><p>{body}</p></article></body></html>"""

    article = extract(html, f"{HOST}/2026/07/25/x")

    assert article is not None and article.title == "Nine first impressions of the prices"


def test_a_site_name_suffix_is_trimmed_off_the_title_fallback():
    body = "Haaland is the obvious captain this week. " * 20
    html = f"""<!doctype html><html><head>
    <title>Best value FPL players for 2026/27 | Fantasy Football Scout</title>
    </head><body><div><p>{body}</p></div></body></html>"""

    article = extract(html, f"{HOST}/2026/07/25/x")

    assert article is not None and article.title == "Best value FPL players for 2026/27"


def test_a_headline_that_contains_a_dash_survives():
    """The suffix trim is timid on purpose: what is left has to look like a
    headline in its own right."""
    body = "Haaland is the obvious captain this week. " * 20
    html = f"""<!doctype html><html><head><title>Salah - or Haaland?</title>
    </head><body><div><p>{body}</p></div></body></html>"""

    article = extract(html, f"{HOST}/2026/07/25/x")

    assert article is not None and article.title == "Salah - or Haaland?"


def test_a_page_with_no_title_anywhere_falls_back_to_its_url():
    body = "Haaland is the obvious captain this week. " * 20
    article = extract(f"<html><body><div><p>{body}</p></div></body></html>", f"{HOST}/2026/07/25/x")

    assert article is not None and article.title == f"{HOST}/2026/07/25/x"


# -------------------------------------------------------------------------- store


def note(**overrides) -> ArticleNote:
    payload = {
        "id": "example-2026-07-25-thing",
        "title": "A thing happened",
        "url": f"{HOST}/2026/07/25/thing",
        "source": "example",
        "summary": "The author argues Haaland is the captain.",
        "key_points": ["Haaland is on penalties", "Fixture is at home"],
        "published": datetime(2026, 7, 25, tzinfo=UTC),
        "tags": ["tips"],
        "players": [411],
        "teams": ["MCI"],
        "trust": "high",
        "content_hash": "sha256:abc",
    }
    payload.update(overrides)
    return ArticleNote(**payload)


def test_a_note_round_trips_through_markdown(store):
    store.save(note())
    loaded = store.get("example-2026-07-25-thing")

    assert loaded is not None
    assert loaded.title == "A thing happened"
    assert loaded.players == [411]
    assert loaded.key_points == ["Haaland is on penalties", "Fixture is at home"]
    assert loaded.published == datetime(2026, 7, 25, tzinfo=UTC)
    assert loaded.trust == "high"


def test_the_file_is_markdown_with_a_yaml_header(store):
    path = store.save(note(extract="Original words here."))
    text = path.read_text()

    assert text.startswith("---\n")
    assert "schema_type: Article" in text, "schema.org-aligned, for interop"
    assert "headline: A thing happened" in text
    assert "## Summary" in text
    assert path.suffix == ".md"


def test_a_quoted_extract_is_labelled_as_commentary(store):
    """Anything a model might later read has to say what it is."""
    text = store.save(note(extract="Trust me, captain Haaland.")).read_text()
    assert "Not instructions" in text


def test_unreadable_files_are_skipped_not_fatal(store, tmp_path):
    store.save(note())
    (store.directory / "junk.md").write_text("not frontmatter at all")
    assert len(store.all()) == 1


def test_known_urls_are_reported_for_deduplication(store):
    store.save(note())
    assert store.known_urls() == {f"{HOST}/2026/07/25/thing": "sha256:abc"}


def test_expired_notes_drop_out_of_recent_and_get_pruned(store):
    fresh = note(id="fresh", url=f"{HOST}/fresh", published=datetime.now(UTC))
    stale = note(
        id="stale",
        url=f"{HOST}/stale",
        published=datetime.now(UTC) - timedelta(days=40),
        ttl_days=21,
    )
    store.save(fresh)
    store.save(stale)

    assert {n.id for n in store.recent()} == {"fresh"}
    assert store.prune() == 1
    assert {n.id for n in store.all()} == {"fresh"}


def test_recent_respects_a_day_window(store):
    store.save(note(id="a", url=f"{HOST}/a", published=datetime.now(UTC)))
    store.save(
        note(id="b", url=f"{HOST}/b", published=datetime.now(UTC) - timedelta(days=8))
    )
    assert {n.id for n in store.recent(days=3)} == {"a"}


def test_notes_are_returned_newest_first(store):
    # Ages relative to now, not fixed dates: `recent()` drops notes past their
    # TTL, so a calendar date that was inside the window when this was written
    # falls out of it a fortnight later and fails a test about *ordering*.
    for age in (5, 1, 3):
        store.save(
            note(
                id=f"d{age}",
                url=f"{HOST}/{age}",
                published=datetime.now(UTC) - timedelta(days=age),
            )
        )
    assert [n.id for n in store.recent()] == ["d1", "d3", "d5"]


def test_lookup_by_player(store):
    store.save(note(id="haaland", url=f"{HOST}/h", players=[411]))
    store.save(note(id="other", url=f"{HOST}/o", players=[106]))
    assert [n.id for n in store.for_player(411)] == ["haaland"]
    assert store.for_player(999) == []


def test_search_matches_title_summary_and_claims(store):
    store.save(note(id="a", url=f"{HOST}/a", title="Captaincy special"))
    store.save(note(id="b", url=f"{HOST}/b", title="Nothing", summary="", key_points=[]))
    assert [n.id for n in store.search("captaincy")] == ["a"]
    assert store.search("") == []


def test_an_index_line_says_when_an_article_was_cut_off(store):
    assert "partial" in note(access="partial").index_line()
    assert "partial" not in note(access="full").index_line()


def test_ids_are_stable_readable_and_filesystem_safe():
    first = make_id("example", f"{HOST}/2026/07/25/some-long-headline", datetime(2026, 7, 25))
    assert first == make_id("example", f"{HOST}/2026/07/25/some-long-headline", datetime(2026, 7, 25))
    assert first == "example-2026-07-25-some-long-headline"
    assert "/" not in first


def test_content_hash_detects_a_changed_article():
    assert content_hash("one") == content_hash("one")
    assert content_hash("one") != content_hash("two")


# ------------------------------------------------------------- player resolution


def test_a_full_name_resolves_to_the_right_player(context):
    """The regression: a fuzzy scorer matched 'Borges Rodrigues' to a different
    player entirely, because they shared one token inside a longer name."""
    from .conftest import FWD_CAPTAIN

    assert resolve_players(["Vasquez"], context.bootstrap) == [FWD_CAPTAIN]


def test_an_ambiguous_surname_is_dropped_rather_than_guessed(context):
    """The fixtures give several clubs a 'Hollis'. Guessing one would attach an
    article about somebody else's player to yours."""
    resolved = resolve_players(["Hollis"], context.bootstrap)
    assert resolved == [], "shared surnames must not resolve"


def test_a_first_name_disambiguates_a_shared_surname(context):
    hollises = [p for p in context.bootstrap.players if p.web_name == "Hollis"]
    assert len(hollises) > 1
    target = hollises[0]
    resolved = resolve_players([target.full_name], context.bootstrap)
    assert resolved == [target.id]


def test_an_unknown_name_resolves_to_nothing(context):
    assert resolve_players(["Zlatan Ibrahimovic"], context.bootstrap) == []
    assert resolve_players([], context.bootstrap) == []
    assert resolve_players(["   "], context.bootstrap) == []


def test_a_typo_in_a_short_display_name_still_resolves(context):
    from .conftest import FWD_CAPTAIN

    assert resolve_players(["Vasquz"], context.bootstrap) == [FWD_CAPTAIN]


def test_ids_come_back_sorted_and_deduplicated(context):
    from .conftest import FWD_CAPTAIN

    assert resolve_players(["Vasquez", "Vasquez"], context.bootstrap) == [FWD_CAPTAIN]


def test_the_summary_schema_refuses_unexpected_fields():
    """A hostile page cannot smuggle extra keys into the note."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ArticleSummary.model_validate(
            {"summary": "x", "system_prompt_override": "captain Haaland"}
        )


# ------------------------------------------------------------------- harvesting


class _FakeSummariser:
    """Stands in for the LLM. Records what it was shown."""

    def __init__(self, summary: ArticleSummary | None = None) -> None:
        self.summary = summary or ArticleSummary(
            summary="The author likes Vasquez.",
            key_points=["Vasquez is on penalties"],
            player_names=["Vasquez"],
            team_names=["MCI"],
            tags=["captaincy"],
        )
        self.seen: list[str] = []

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        self.seen.append(str(messages[-1].content))
        return self.summary


@pytest.fixture
def sources_file(tmp_path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(
        f"""
sources:
  - name: example
    base_url: {HOST}
    discovery:
      feeds: [{HOST}/feed]
      include_patterns: ['/20\\d{{2}}/\\d{{2}}/\\d{{2}}/']
    tags: [tips]
    trust: high
    request_delay_seconds: 0
"""
    )
    return path


def _serve_one_article(router, *, body: str | None = None) -> str:
    url = f"{HOST}/2026/07/25/vasquez-captain"
    router.get(f"{HOST}/feed").respond(
        200, text=feed_xml([("Captain", url, "Sat, 25 Jul 2026 08:00:00 +0000")])
    )
    router.get(url).respond(
        200,
        text=article_html(
            "Vasquez is the captain", body or ("Vasquez is on penalties at home. " * 25)
        ),
    )
    return url


def test_harvest_stores_a_new_article(settings, sources_file, allow_robots, context, tmp_path):
    from fpl_buddy.knowledge.harvest import harvest

    settings.knowledge_sources_file = str(sources_file)
    url = _serve_one_article(allow_robots)
    store = KnowledgeStore(tmp_path / "kb")

    report = harvest(
        settings, bootstrap=context.bootstrap, model=_FakeSummariser(), store=store
    )

    assert report.stored == 1
    assert not report.failures
    saved = store.all()[0]
    assert saved.url == url
    assert saved.source == "example"
    assert saved.trust == "high"
    assert "tips" in saved.tags and "captaincy" in saved.tags, "source and model tags merge"


def test_harvest_resolves_mentioned_players_to_ids(
    settings, sources_file, allow_robots, context, tmp_path
):
    from fpl_buddy.knowledge.harvest import harvest

    from .conftest import FWD_CAPTAIN

    settings.knowledge_sources_file = str(sources_file)
    _serve_one_article(allow_robots)
    store = KnowledgeStore(tmp_path / "kb")

    harvest(settings, bootstrap=context.bootstrap, model=_FakeSummariser(), store=store)
    assert store.all()[0].players == [FWD_CAPTAIN]


def test_harvest_does_not_restore_something_it_already_has(
    settings, sources_file, allow_robots, context, tmp_path
):
    from fpl_buddy.knowledge.harvest import harvest

    settings.knowledge_sources_file = str(sources_file)
    _serve_one_article(allow_robots)
    store = KnowledgeStore(tmp_path / "kb")

    first = harvest(settings, bootstrap=context.bootstrap, model=_FakeSummariser(), store=store)
    second = harvest(settings, bootstrap=context.bootstrap, model=_FakeSummariser(), store=store)

    assert first.stored == 1
    assert second.stored == 0
    assert second.skipped_known == 1
    assert len(store.all()) == 1


def test_harvest_marks_a_paywalled_article_partial(
    settings, sources_file, allow_robots, context, tmp_path
):
    from fpl_buddy.knowledge.harvest import harvest

    settings.knowledge_sources_file = str(sources_file)
    _serve_one_article(
        allow_robots,
        body="Here is the intro. " * 30 + " This content is restricted to Chief Scout Members.",
    )
    store = KnowledgeStore(tmp_path / "kb")

    report = harvest(settings, bootstrap=context.bootstrap, model=_FakeSummariser(), store=store)

    assert report.partial == 1
    saved = store.all()[0]
    assert saved.access == "partial"
    assert saved.partial_reason == "paywalled"


def test_a_long_article_we_truncated_is_not_called_paywalled(
    settings, sources_file, allow_robots, context, tmp_path
):
    """A free article longer than the input budget is incomplete for our own
    reasons. Telling the agent it was gated is simply false -- and only one of
    the two is fixable with a subscription."""
    from fpl_buddy.knowledge.harvest import harvest
    from fpl_buddy.knowledge.summarize import MAX_INPUT_CHARS

    settings.knowledge_sources_file = str(sources_file)
    _serve_one_article(allow_robots, body="Words and words. " * (MAX_INPUT_CHARS // 8))
    model = _FakeSummariser(
        ArticleSummary(summary="Long piece.", truncated=True, player_names=[])
    )
    store = KnowledgeStore(tmp_path / "kb")

    report = harvest(settings, bootstrap=context.bootstrap, model=model, store=store)

    saved = store.all()[0]
    assert saved.access == "partial"
    assert "budget" in saved.partial_reason
    assert "paywall" not in saved.partial_reason
    assert report.partial == 0, "the paywall counter must not count our own truncation"
    assert "paywalled" not in saved.index_line()


def test_a_short_article_that_stops_dead_is_attributed_to_the_source(
    settings, sources_file, allow_robots, context, tmp_path
):
    """A freemium site can strip its own "restricted to members" notice as
    boilerplate, leaving text that just stops. Nothing marks it, and it is far
    below our input budget -- so the source did the cutting, not us."""
    from fpl_buddy.knowledge.harvest import harvest

    settings.knowledge_sources_file = str(sources_file)
    _serve_one_article(allow_robots, body="Short and abruptly ending. " * 25)
    model = _FakeSummariser(
        ArticleSummary(summary="Stops mid-section.", truncated=True, player_names=[])
    )
    store = KnowledgeStore(tmp_path / "kb")

    report = harvest(settings, bootstrap=context.bootstrap, model=model, store=store)

    saved = store.all()[0]
    assert saved.access == "partial"
    assert "source" in saved.partial_reason
    assert report.partial == 1, "attributed to the publisher, so it counts as paywalled"


def test_the_article_text_reaches_the_summariser_labelled_untrusted(
    settings, sources_file, allow_robots, context, tmp_path
):
    """The trust boundary has to be visible in the prompt itself."""
    from fpl_buddy.knowledge.harvest import harvest

    settings.knowledge_sources_file = str(sources_file)
    _serve_one_article(allow_robots)
    model = _FakeSummariser()

    harvest(settings, bootstrap=context.bootstrap, model=model, store=KnowledgeStore(tmp_path / "k"))

    assert "BEGIN UNTRUSTED ARTICLE TEXT" in model.seen[0]
    assert "END UNTRUSTED ARTICLE TEXT" in model.seen[0]


def test_harvest_without_configured_sources_does_nothing(settings, tmp_path):
    from fpl_buddy.knowledge.harvest import harvest

    settings.knowledge_sources_file = ""
    report = harvest(settings, store=KnowledgeStore(tmp_path / "kb"))
    assert report.stored == 0 and report.considered == 0


def test_one_broken_source_does_not_stop_the_others(
    settings, tmp_path, allow_robots, context
):
    path = tmp_path / "s.yaml"
    path.write_text(
        f"""
sources:
  - name: broken
    base_url: https://broken.test
    discovery:
      feeds: [https://broken.test/feed]
    request_delay_seconds: 0
  - name: example
    base_url: {HOST}
    discovery:
      feeds: [{HOST}/feed]
      include_patterns: ['/20\\d{{2}}/']
    request_delay_seconds: 0
"""
    )
    from fpl_buddy.knowledge.harvest import harvest

    settings.knowledge_sources_file = str(path)
    allow_robots.get("https://broken.test/feed").mock(side_effect=httpx.ConnectError("down"))
    _serve_one_article(allow_robots)
    store = KnowledgeStore(tmp_path / "kb")

    report = harvest(settings, bootstrap=context.bootstrap, model=_FakeSummariser(), store=store)
    assert report.stored == 1, "the healthy source still ran"


def test_a_summariser_failure_costs_one_article_not_the_run(
    settings, sources_file, allow_robots, context, tmp_path
):
    from fpl_buddy.knowledge.harvest import harvest

    class _Broken(_FakeSummariser):
        def invoke(self, messages):
            raise RuntimeError("model is down")

    settings.knowledge_sources_file = str(sources_file)
    _serve_one_article(allow_robots)
    store = KnowledgeStore(tmp_path / "kb")

    report = harvest(settings, bootstrap=context.bootstrap, model=_Broken(), store=store)
    assert report.stored == 0
    assert report.failures, "the failure is reported rather than swallowed silently"
    assert store.all() == []


# --------------------------------------------------------------------- backends
#
# The real SDKs are optional installs and absent in CI, so nothing here imports
# them. Backends are driven through injected fakes, which is also the only way
# to exercise credit exhaustion without spending credits.


class _FakeFirecrawlDoc:
    def __init__(self, markdown="", html="", title="", author="", published=""):
        self.markdown = markdown
        self.html = html
        self.metadata = {"title": title, "author": author, "publishedTime": published}


class _FakeFirecrawlClient:
    def __init__(self, *, remaining=1000, doc=None, error=None):
        self.remaining = remaining
        self.doc = doc if doc is not None else _FakeFirecrawlDoc(html="<article>hi</article>")
        self.error = error
        self.scrapes: list[str] = []

    def get_credit_usage(self):
        return {"remaining_credits": self.remaining}

    def scrape(self, url, **kwargs):
        if self.error:
            raise self.error
        self.scrapes.append(url)
        return self.doc


def _firecrawl(settings, client):
    """A FirecrawlBackend wired to a fake client, skipping the real import."""
    from fpl_buddy.knowledge.backends import FirecrawlBackend

    backend = FirecrawlBackend(settings)
    backend._client = client
    backend._credits_left = client.remaining
    return backend


def test_firecrawl_is_skipped_without_a_key(settings):
    from fpl_buddy.knowledge.backends import FirecrawlBackend

    assert not settings.firecrawl_api_key.get_secret_value()
    assert FirecrawlBackend(settings).available() is False


def test_firecrawl_returns_both_html_and_markdown(settings):
    client = _FakeFirecrawlClient(
        doc=_FakeFirecrawlDoc(markdown="# hi", html="<article>hi</article>", title="Hi")
    )
    content = _firecrawl(settings, client).fetch(f"{HOST}/a", make_source())

    assert content is not None
    assert content.backend == "firecrawl"
    assert content.html == "<article>hi</article>"
    assert content.markdown == "# hi"
    assert content.title == "Hi", "metadata must survive -- it is an object, not a string"


def test_firecrawl_stops_at_the_credit_reserve(settings):
    """The free tier is finite; a harvest must not spend the last of it."""
    settings.firecrawl_credit_reserve = 50
    client = _FakeFirecrawlClient(remaining=51)
    backend = _firecrawl(settings, client)

    assert backend.fetch(f"{HOST}/a", make_source()) is not None
    assert backend.fetch(f"{HOST}/b", make_source()) is None, "reserve reached"
    assert len(client.scrapes) == 1


def test_firecrawl_failure_falls_through_rather_than_raising(settings):
    client = _FakeFirecrawlClient(error=RuntimeError("upstream is down"))
    assert _firecrawl(settings, client).fetch(f"{HOST}/a", make_source()) is None


def test_firecrawl_passes_a_configured_subscription_cookie(settings, monkeypatch):
    """Authenticated access to content you pay for, not a paywall bypass."""
    monkeypatch.setenv("TEST_COOKIE", "session=abc")
    seen = {}

    class _Recording(_FakeFirecrawlClient):
        def scrape(self, url, **kwargs):
            seen.update(kwargs)
            return self.doc

    _firecrawl(settings, _Recording()).fetch(
        f"{HOST}/a", make_source(cookie_env="TEST_COOKIE")
    )
    assert seen["headers"]["Cookie"] == "session=abc"
    assert "html" in seen["formats"] and "markdown" in seen["formats"]


def test_an_empty_firecrawl_document_is_not_usable(settings):
    client = _FakeFirecrawlClient(doc=_FakeFirecrawlDoc(markdown="  ", html=""))
    assert _firecrawl(settings, client).fetch(f"{HOST}/a", make_source()) is None


def test_scrapling_is_skipped_when_not_installed(settings, monkeypatch):
    import builtins

    from fpl_buddy.knowledge.backends import ScraplingBackend

    real_import = builtins.__import__

    def no_scrapling(name, *args, **kwargs):
        if name.startswith("scrapling"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_scrapling)
    assert ScraplingBackend(settings).available() is False


def test_scrapling_returns_html_and_rejects_non_200(settings):
    from fpl_buddy.knowledge.backends import ScraplingBackend

    class _Response:
        def __init__(self, status, html):
            self.status = status
            self.html_content = html

    class _Fetcher:
        def __init__(self, response):
            self.response = response

        def get(self, url, **kwargs):
            return self.response

    ok = ScraplingBackend(settings)
    ok._fetcher = _Fetcher(_Response(200, "<article>hello</article>"))
    content = ok.fetch(f"{HOST}/a", make_source())
    assert content is not None and content.backend == "scrapling"

    blocked = ScraplingBackend(settings)
    blocked._fetcher = _Fetcher(_Response(403, "denied"))
    assert blocked.fetch(f"{HOST}/a", make_source()) is None


def test_httpx_backend_is_always_available(settings, fetcher):
    from fpl_buddy.knowledge.backends import HttpxBackend

    assert HttpxBackend(fetcher).available() is True


def test_the_chain_falls_through_to_the_next_backend(settings, fetcher):
    from fpl_buddy.knowledge.backends import ArticleContent, Backend, fetch_article

    class _Dead(Backend):
        name = "dead"

        def fetch(self, url, source):
            return None

    class _Works(Backend):
        name = "works"

        def fetch(self, url, source):
            return ArticleContent(url=url, html="<article>x</article>", backend=self.name)

    got = fetch_article(f"{HOST}/a", make_source(), [_Dead(), _Works()])
    assert got is not None and got.backend == "works"


def test_the_chain_returns_nothing_when_every_backend_fails(settings):
    from fpl_buddy.knowledge.backends import Backend, fetch_article

    class _Dead(Backend):
        name = "dead"

        def fetch(self, url, source):
            return None

    assert fetch_article(f"{HOST}/a", make_source(), [_Dead()]) is None


def test_backend_order_comes_from_config(settings, fetcher):
    from fpl_buddy.knowledge.backends import build_backends

    settings.knowledge_fetch_backends = "httpx"
    assert [b.name for b in build_backends(settings, fetcher)] == ["httpx"]


def test_an_unknown_backend_name_is_ignored_not_fatal(settings, fetcher):
    from fpl_buddy.knowledge.backends import build_backends

    settings.knowledge_fetch_backends = "nonsense,httpx"
    assert [b.name for b in build_backends(settings, fetcher)] == ["httpx"]


def test_there_is_always_a_backend_even_if_config_names_none(settings, fetcher):
    """Asserted by shape, not by exact list: whether scrapling is installed is a
    property of the machine, and this must hold either way."""
    from fpl_buddy.knowledge.backends import build_backends

    settings.knowledge_fetch_backends = ""
    names = [b.name for b in build_backends(settings, fetcher)]
    assert names, "a harvest with no way to fetch anything is not a useful state"
    assert names[-1] == "httpx", "the always-available backend stays last"


# ------------------------------------------------- rendered-markdown handling


def test_a_browser_interstitial_is_trimmed_off_rendered_markdown():
    """A rendering backend sees consent walls and extension blocks that a plain
    HTTP client never does, and they arrive ahead of the article."""
    from fpl_buddy.knowledge.extract import from_markdown

    markdown = (
        "edigitalsurvey.com\n\n# edigitalsurvey.com is blocked\n\n"
        "This page has been blocked by an extension\n\nERR_BLOCKED_BY_CLIENT\n\n"
        "Reload\n\n" + "Newcastle beat Arsenal to the signing. " * 30
    )
    article = from_markdown(markdown, f"{HOST}/a", title="Real headline")

    assert article is not None
    assert "ERR_BLOCKED" not in article.text
    assert "blocked by an extension" not in article.text
    assert article.text.startswith("Newcastle beat Arsenal")


def test_markdown_syntax_is_reduced_to_prose():
    from fpl_buddy.knowledge.extract import from_markdown

    markdown = (
        "## Heading\n\n[Read more](https://example.test/x) and **bold** text. "
        "![img](https://example.test/i.png)\n\n" + "Body sentence here. " * 30
    )
    article = from_markdown(markdown, f"{HOST}/a")

    assert article is not None
    assert "https://example.test" not in article.text, "link targets are noise"
    assert "Read more" in article.text, "but their labels are content"
    assert "**" not in article.text and "##" not in article.text


def test_a_paywall_marker_in_rendered_markdown_is_still_caught():
    from fpl_buddy.knowledge.extract import from_markdown

    markdown = "Intro paragraph. " * 40 + "\n\nThis content is restricted to members."
    article = from_markdown(markdown, f"{HOST}/a")
    assert article is not None and article.access == "partial"


# ----------------------------------------------------------------------- youtube
#
# youtube-transcript-api is an optional install and absent in CI, so the
# transcript path is driven through injected fakes.


YT_FEED = """<?xml version="1.0"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry><yt:videoId>aaaaaaaaaaa</yt:videoId><title>Best FPL picks for GW1</title>
    <published>2026-07-25T10:00:00+00:00</published></entry>
  <entry><yt:videoId>bbbbbbbbbbb</yt:videoId><title>Quick thought #shorts</title>
    <published>2026-07-24T10:00:00+00:00</published></entry>
  <entry><yt:videoId>ccccccccccc</yt:videoId><title>LIVE Q&amp;A stream</title>
    <published>2026-07-23T10:00:00+00:00</published></entry>
</feed>"""


def youtube_source(**overrides) -> Source:
    payload = {
        "name": "channel",
        "kind": "youtube",
        "channel": "UCHcvyjfCHf5D1RmVc216qWA",
        "ignore_robots": True,
        "discovery": {"exclude_patterns": ["(?i)#shorts", r"(?i)\bLIVE\b"]},
        "request_delay_seconds": 0,
    }
    payload.update(overrides)
    return Source.model_validate(payload)


def test_a_youtube_source_needs_a_channel():
    with pytest.raises(ValueError, match="needs a channel"):
        Source.model_validate({"name": "x", "kind": "youtube"})


def test_a_channel_makes_no_sense_on_an_article_source():
    with pytest.raises(ValueError, match="only applies to kind 'youtube'"):
        Source.model_validate({"name": "x", "base_url": HOST, "channel": "@someone"})


def test_a_youtube_source_without_the_robots_opt_out_is_warned_about(caplog):
    """Both the feed and the caption endpoint are disallowed, so it would
    silently find nothing."""
    youtube_source(ignore_robots=False)
    assert "robots.txt" in caplog.text


def test_ignore_robots_is_per_source_and_off_by_default(settings):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r".*/robots\.txt").respond(
            200, text="User-agent: *\nDisallow: /feeds/"
        )
        page = router.get("https://www.youtube.com/feeds/videos.xml").respond(200, text="ok")
        fetcher = Fetcher(settings)

        blocked = fetcher.get("https://www.youtube.com/feeds/videos.xml", source=make_source())
        assert not blocked.ok and not page.called, "default still honours robots"

        allowed = fetcher.get(
            "https://www.youtube.com/feeds/videos.xml", source=youtube_source()
        )
        assert allowed.ok and page.called


def test_channel_ids_resolve_from_every_accepted_form():
    from fpl_buddy.knowledge.youtube import resolve_channel_id

    uc = "UCHcvyjfCHf5D1RmVc216qWA"
    assert resolve_channel_id(uc, lambda url: "") == uc
    assert resolve_channel_id(f"https://www.youtube.com/channel/{uc}", lambda url: "") == uc

    fetched = []

    def page(url):
        fetched.append(url)
        return f'<link rel="canonical" href="https://www.youtube.com/channel/{uc}">'

    assert resolve_channel_id("@SomeChannel", page) == uc
    assert fetched == ["https://www.youtube.com/@SomeChannel"]


def test_the_channels_own_id_wins_over_any_other_on_the_page():
    """The bug this pins: taking the first UC-looking id on a channel page
    resolved @LetsTalkFPL to "Let's Talk Football", a different channel, and
    every downstream step then looked perfectly healthy while harvesting
    somebody else's uploads."""
    from fpl_buddy.knowledge.youtube import resolve_channel_id

    mine, someone_else = "UCxeOc7eFxq37yW_Nc-69deA", "UCHcvyjfCHf5D1RmVc216qWA"
    page = (
        f'{{"channelId":"{someone_else}","recommended":true}}'
        f'<link rel="canonical" href="https://www.youtube.com/channel/{mine}">'
        f'{{"externalId":"{mine}"}}'
    )
    assert resolve_channel_id("@Someone", lambda url: page) == mine


def test_a_page_without_a_canonical_id_resolves_to_nothing():
    """Guessing is what caused the bug above, so there is no fallback."""
    from fpl_buddy.knowledge.youtube import resolve_channel_id

    page = '{"channelId":"UCHcvyjfCHf5D1RmVc216qWA","recommended":true}'
    assert resolve_channel_id("@Someone", lambda url: page) is None


def test_an_unresolvable_channel_returns_nothing():
    from fpl_buddy.knowledge.youtube import resolve_channel_id

    assert resolve_channel_id("@Missing", lambda url: "") is None
    assert resolve_channel_id("@Missing", lambda url: "<html>no ids here</html>") is None


def test_video_ids_are_read_from_urls_and_bare_ids():
    from fpl_buddy.knowledge.youtube import video_id_from_url

    assert video_id_from_url("https://www.youtube.com/watch?v=aaaaaaaaaaa") == "aaaaaaaaaaa"
    assert video_id_from_url("https://youtu.be/aaaaaaaaaaa") == "aaaaaaaaaaa"
    assert video_id_from_url("aaaaaaaaaaa") == "aaaaaaaaaaa"
    assert video_id_from_url("https://www.youtube.com/") is None


def test_the_upload_feed_becomes_dated_candidates(settings, fetcher, allow_robots):
    allow_robots.get(url__regex=r".*/feeds/videos\.xml.*").respond(200, text=YT_FEED)
    found = discover(youtube_source(), fetcher)

    assert [c.url for c in found] == ["https://www.youtube.com/watch?v=aaaaaaaaaaa"]
    assert found[0].title == "Best FPL picks for GW1"
    assert found[0].published.day == 25


def test_youtube_patterns_filter_on_the_title_not_the_url(settings, fetcher, allow_robots):
    """A watch URL is an opaque id, so the title is the only signal for
    skipping shorts and streams."""
    allow_robots.get(url__regex=r".*/feeds/videos\.xml.*").respond(200, text=YT_FEED)
    urls = [c.url for c in discover(youtube_source(), fetcher)]

    assert "https://www.youtube.com/watch?v=bbbbbbbbbbb" not in urls, "#shorts"
    assert "https://www.youtube.com/watch?v=ccccccccccc" not in urls, "LIVE stream"


def test_a_blocked_feed_explains_what_is_missing(settings, fetcher, caplog):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r".*/robots\.txt").respond(200, text="User-agent: *\nDisallow: /")
        assert discover(youtube_source(ignore_robots=False), fetcher) == []
    assert "ignore_robots" in caplog.text


def test_timestamps_are_marked_through_the_transcript():
    from fpl_buddy.knowledge.youtube import _with_timestamps

    class _Snippet:
        def __init__(self, start, text):
            self.start = start
            self.text = text

        duration = 3.0

    text = _with_timestamps(
        [_Snippet(0, "hello there"), _Snippet(30, "still talking"), _Snippet(75, "much later")]
    )
    assert "[0:00]" in text
    assert "[1:15]" in text, "a marker roughly once a minute"
    assert "[0:30]" not in text, "not one per segment"
    assert "hello there" in text and "much later" in text


def test_a_transcript_becomes_an_article_without_extraction():
    from fpl_buddy.knowledge.extract import from_transcript
    from fpl_buddy.knowledge.youtube import Transcript

    transcript = Transcript(video_id="aaaaaaaaaaa", text="Some FPL talk. " * 50)
    article = from_transcript(transcript, "Best picks", author="channel")

    assert article is not None
    assert article.url == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert article.title == "Best picks"
    assert article.access == "full", "a video is not paywalled the way an article is"
    assert article.usable


def test_a_video_note_declares_itself_a_videoobject(store):
    """schema.org has a type for this, and the header is meant to be portable."""
    note = ArticleNote(
        id="channel-2026-07-25-aaaaaaaaaaa",
        title="Best picks",
        url="https://www.youtube.com/watch?v=aaaaaaaaaaa",
        source="channel",
        kind="youtube",
        video_id="aaaaaaaaaaa",
    )
    text = store.save(note).read_text()
    assert "schema_type: VideoObject" in text
    assert "video_id: aaaaaaaaaaa" in text

    loaded = store.get("channel-2026-07-25-aaaaaaaaaaa")
    assert loaded.kind == "youtube"
    assert loaded.video_id == "aaaaaaaaaaa"


def test_an_article_note_is_still_an_article(store):
    store.save(note())
    assert "schema_type: Article" in store.path_for(note()).read_text()


def test_a_video_id_is_used_as_the_filename_slug():
    """Slugifying a watch URL gives "watch-v-aaaaaaaaaaa", which is nobody's
    idea of a readable id."""
    from datetime import UTC, datetime

    made = make_id(
        "channel",
        "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        datetime(2026, 7, 25, tzinfo=UTC),
        slug="aaaaaaaaaaa",
    )
    assert made == "channel-2026-07-25-aaaaaaaaaaa"
