"""The decision brief.

The brief is the agent's whole view of the world, so the facts it needs must
actually be in there: selling prices, injury flags, ids to quote back, and an
explicit warning about projection rows that could not be matched to an id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl_buddy.data.context import build_context

from .conftest import (
    DEF_INJURED,
    FWD_CAPTAIN,
    GK_RESERVE,
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
    row = next(
        line for line in context.squad_table().splitlines() if f"id={FWD_CAPTAIN}" in line
    )
    assert row.split("|")[7].strip() != "-", "the projection column should have a number"


# ------------------------------------------------------------------- building


def test_build_context_assembles_everything(settings, fake_client, mock_solio):
    context = build_context(settings, fake_client)

    assert context.gameweek.id == NEXT_GAMEWEEK
    assert len(context.my_team.picks) == 15
    assert len(context.fixtures) == 3
    assert context.solio is not None
    assert context.solio_unmatched, "the fixture deliberately contains unmatchable rows"
    assert context.solio.projection_for(FWD_CAPTAIN) is not None


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
