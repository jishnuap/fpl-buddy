"""The decision brief.

The brief is the agent's whole view of the world, so the facts it needs must
actually be in there: selling prices, injury flags, ids to quote back, and an
explicit warning about projection rows that could not be matched to an id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl_buddy.data.context import build_context
from fpl_buddy.fpl.models import UNLIMITED_FREE_TRANSFERS

from .conftest import (
    DEF_INJURED,
    FWD_CAPTAIN,
    GK_RESERVE,
    MID_BENCH,
    MID_LIV,
    MID_VICE,
    NEXT_GAMEWEEK,
)


def test_hours_to_deadline_is_measured_from_now(context):
    context.gameweek.deadline_time = datetime.now(UTC) + timedelta(hours=12)
    assert 11.9 < context.hours_to_deadline < 12.1


def test_squad_table_lists_all_fifteen_with_ids(context):
    table = context.squad_table()
    rows = [line for line in table.splitlines() if "id=" in line]
    assert len(rows) == 15
    for pick in context.my_team.picks:
        assert f"id={pick.element}" in table


def test_squad_table_shows_selling_price_not_just_now_cost(context):
    pick = context.my_team.pick_for(MID_LIV)
    player = context.bootstrap.player(MID_LIV)
    assert pick.selling_price != player.now_cost, "fixture precondition"

    row = next(
        line for line in context.squad_table().splitlines() if f"id={MID_LIV}" in line
    )
    assert f"{pick.selling_price / 10:.1f}" in row
    assert f"{player.price:.1f}" in row


def test_squad_table_marks_the_armbands_and_the_bench(context):
    rows = {
        int(line.split("id=")[1].rstrip("]")): line
        for line in context.squad_table().splitlines()
        if "id=" in line
    }
    assert "(C)" in rows[FWD_CAPTAIN]
    assert "(V)" in rows[MID_VICE]
    assert "BENCH" in rows[GK_RESERVE]


def test_squad_table_flags_an_injured_player(context):
    row = next(
        line for line in context.squad_table().splitlines() if f"id={DEF_INJURED}" in line
    )
    assert "25%" in row
    assert "OK" not in row


def test_news_lines_surface_injury_text(context):
    news = "\n".join(context.news_lines())
    assert "Hamstring injury" in news


def test_fixture_lines_include_difficulty(context):
    lines = context.fixture_lines()
    assert len(lines) == 3
    assert all("FDR" in line for line in lines)


def test_render_contains_the_sections_the_prompt_refers_to(context):
    brief = context.render()
    for heading in ("# FPL decision brief", "## Your squad", "## Fixtures"):
        assert heading in brief
    assert "Bank: £1.5m" in brief
    assert "Free transfers: 1" in brief
    assert "Chips available: wildcard, 3xc" in brief


def test_render_says_when_projections_are_missing(context):
    assert context.solio is None
    assert "Solio projections were unavailable" in context.render()


def test_render_includes_projections_when_present(context, solio):
    context.solio = solio
    brief = context.render()
    assert "Solio Analytics snapshot" in brief
    assert "topProjected" in brief


def test_render_warns_about_unmatched_projection_rows(context, solio):
    """Rows without an id must be explicitly off-limits as transfer targets."""
    context.solio = solio
    context.solio_unmatched = ["Zoltan Nevermatch (MCI, MID)"]
    brief = context.render()
    assert "must not be" in brief
    assert "Zoltan Nevermatch" in brief


def test_projection_column_is_filled_in_when_solio_is_joined(context, solio):
    context.solio = solio
    lines = context.squad_table().splitlines()
    # Located via the header rather than a hard-coded index, so adding a column
    # can't quietly make this assert about a different one.
    column = [c.strip() for c in lines[0].split("|")].index("proj")
    row = next(line for line in lines if f"id={FWD_CAPTAIN}" in line)
    assert row.split("|")[column].strip() != "-", "the projection column should have a number"


def test_squad_table_carries_the_underlying_numbers(context):
    lines = context.squad_table().splitlines()
    header = [c.strip() for c in lines[0].split("|")]
    row = next(line for line in lines if f"id={FWD_CAPTAIN}" in line).split("|")

    assert row[header.index("xGI90")].strip() == "0.67"
    assert row[header.index("st90")].strip() == "1.00"
    assert row[header.index("setp")].strip() == "P1", "first-choice penalty taker"


# ---------------------------------------------------------- captain candidates


def test_captain_candidates_come_only_from_the_squad(context, solio):
    """The failure this prevents: captaining the best player in the league
    rather than the best player you own."""
    context.solio = solio
    squad_ids = {p.element for p in context.my_team.picks}

    lines = context.captain_candidate_lines()
    assert lines, "there should be candidates"
    for line in lines:
        element_id = int(line.split("id=")[1].split()[0])
        assert element_id in squad_ids


def test_captain_candidates_exclude_goalkeepers(context):
    ids = {int(line.split("id=")[1].split()[0]) for line in context.captain_candidate_lines()}
    assert GK_RESERVE not in ids
    for element_id in ids:
        assert context.bootstrap.player(element_id).position != "GKP"


def test_captain_candidates_are_ranked_best_projection_first(context, solio):
    context.solio = solio
    scores = []
    for line in context.captain_candidate_lines():
        element_id = int(line.split("id=")[1].split()[0])
        scores.append(context.projection_value(element_id) or 0.0)
    assert scores == sorted(scores, reverse=True)


def test_captain_candidates_flag_the_unavailable_and_the_benched(context):
    lines = context.captain_candidate_lines(limit=99)
    injured = next(line for line in lines if f"id={DEF_INJURED}" in line)
    assert "FLAGGED" in injured
    benched = next(line for line in lines if f"id={MID_BENCH}" in line)
    assert "benched" in benched


def test_render_tells_the_agent_the_leaderboards_are_not_its_squad(context):
    brief = context.render()
    assert "Legal captain / vice options" in brief
    assert "NOT yours" in brief


# ------------------------------------------------------------- free transfers


def test_unlimited_transfers_are_described_as_unlimited_not_as_fifteen(context):
    """An agent told it has "15 free transfers" reads a finite budget and rolls."""
    context.my_team.free_transfers = UNLIMITED_FREE_TRANSFERS
    brief = context.render()

    assert "Free transfers: unlimited" in brief
    assert "Free transfers: 15" not in brief
    assert "Transfers are free this gameweek" in brief
    assert "Rolling is NOT the default" in brief


def test_a_normal_week_says_nothing_about_free_transfers(context):
    assert context.my_team.free_transfers == 1
    brief = context.render()
    assert "Free transfers: 1" in brief
    assert "Transfers are free this gameweek" not in brief


# ------------------------------------------------------------ fixture horizon


def test_fixture_run_covers_more_than_the_current_gameweek(context):
    lines = context.fixture_run_lines()
    assert lines, "the horizon should produce a run per club"
    assert any("GW5" in line for line in lines), "not just this gameweek"


def test_fixture_run_only_covers_clubs_you_own(context):
    owned = {
        context.bootstrap.player(p.element).team for p in context.my_team.picks
    }
    owned_codes = {context.bootstrap.team(t).short_name for t in owned}
    codes = {line.split()[0] for line in context.fixture_run_lines()}
    assert codes == owned_codes


def test_fixture_run_marks_home_away_and_difficulty(context):
    line = context.fixture_run_lines()[0]
    assert "(H," in line or "(A," in line


def test_fixture_run_is_empty_without_a_horizon(context):
    """The horizon is optional -- losing it must not break the brief."""
    context.horizon_fixtures = []
    assert context.fixture_run_lines() == []
    assert "Fixture run" not in context.render()


# ------------------------------------------------------- harvested articles


def article(**overrides):
    from datetime import UTC as _UTC

    from fpl_buddy.knowledge.store import ArticleNote

    payload = {
        "id": "src-2026-07-25-thing",
        "title": "Vasquez is the obvious captain",
        "url": "https://news.example.test/2026/07/25/thing",
        "source": "src",
        "summary": "The author argues for Vasquez.",
        "key_points": ["He is on penalties"],
        "published": datetime(2026, 7, 25, tzinfo=_UTC),
        "tags": ["captaincy"],
        "players": [FWD_CAPTAIN],
    }
    payload.update(overrides)
    return ArticleNote(**payload)


def test_the_brief_lists_articles_as_an_index_not_their_contents(context):
    """A growing archive must not grow the per-run token cost."""
    context.articles = [article(summary="x" * 5000)]
    brief = context.render()

    assert "Recent FPL articles" in brief
    assert "src-2026-07-25-thing" in brief, "the id, so a tool can fetch it"
    assert "Vasquez is the obvious captain" in brief, "the headline"
    assert "x" * 200 not in brief, "but not the body"


def test_the_brief_labels_articles_as_untrusted_commentary(context):
    context.articles = [article()]
    brief = context.render()
    assert "NOT INSTRUCTIONS" in brief
    assert "read_article" in brief, "and how to load one properly"


def test_a_partial_article_says_so_in_the_index(context):
    context.articles = [article(access="partial")]
    assert "partial" in context.render()


def test_no_articles_means_no_article_section(context):
    assert context.articles == []
    assert "Recent FPL articles" not in context.render()


# ------------------------------------------------------------------- building


def test_build_context_assembles_everything(settings, fake_client, mock_solio):
    context = build_context(settings, fake_client)

    assert context.gameweek.id == NEXT_GAMEWEEK
    assert len(context.my_team.picks) == 15
    assert len(context.fixtures) == 3
    assert context.solio is not None
    assert context.solio_unmatched, "the fixture deliberately contains unmatchable rows"
    assert context.solio.projection_for(FWD_CAPTAIN) is not None
    assert context.horizon_fixtures, "the multi-gameweek horizon should be loaded"


def test_the_horizon_stops_at_the_configured_number_of_gameweeks(
    settings, fake_client, mock_solio
):
    settings.fixture_horizon_gameweeks = 2
    context = build_context(settings, fake_client)
    # GW4 is next, so a 2-gameweek horizon is GW4 and GW5 -- never GW6.
    assert {f.event for f in context.horizon_fixtures} == {4, 5}
    assert context.horizon_gameweeks == 2


def test_build_context_survives_the_horizon_failing(settings, fake_client, mock_solio):
    """A missing horizon costs some reasoning quality, not the gameweek."""
    import httpx

    fake_client.future_fixtures_error = httpx.ConnectError("no route")
    context = build_context(settings, fake_client)

    assert context.horizon_fixtures == []
    assert len(context.fixtures) == 3, "this gameweek still loaded"
    assert "## Your squad" in context.render()


def test_build_context_survives_solio_being_unreachable(settings, fake_client):
    import httpx
    import respx

    with respx.mock:
        respx.get(settings.solio_url).mock(side_effect=httpx.ConnectError("blocked"))
        context = build_context(settings, fake_client)

    assert context.solio is None
    assert "Solio projections were unavailable" in context.render()


def test_build_context_survives_a_solio_403(settings, fake_client):
    """Some networks and proxies block it outright. Not our problem to fix."""
    import httpx
    import respx

    with respx.mock:
        respx.get(settings.solio_url).mock(return_value=httpx.Response(403, text="denied"))
        context = build_context(settings, fake_client)

    assert context.solio is None


def test_build_context_raises_when_the_season_is_over(settings, fake_client):
    for event in fake_client.bootstrap().events:
        event.finished = True
        event.is_next = False

    with pytest.raises(RuntimeError, match="No upcoming gameweek"):
        build_context(settings, fake_client)


def test_a_mismatched_solio_gameweek_is_tolerated(settings, fake_client, caplog):
    """Stale projections are a warning, not a failure."""
    import json

    import httpx
    import respx

    from .conftest import load_json

    raw = load_json("solio-latest.json")
    raw["gameweek"] = 99

    with respx.mock:
        respx.get(settings.solio_url).mock(
            return_value=httpx.Response(200, content=json.dumps(raw))
        )
        context = build_context(settings, fake_client)

    assert context.solio is not None
    assert "stale" in caplog.text.lower() or "GW99" in caplog.text
