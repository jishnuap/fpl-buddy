"""Reading the AIrsenal artefact, and what the brief does with it.

The artefact is written by a container this project does not control, on a
schedule this project does not drive. So most of what matters here is the
failure side: every way the file can be missing, stale, corrupt or about the
wrong gameweek has to end in "run without it and say so", never an exception on
the deadline path and never a silently wrong number.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from fpl_buddy.data.airsenal import (
    SUPPORTED_SCHEMA_VERSION,
    AirsenalSnapshot,
    load_snapshot,
    snapshot_path,
)

from .conftest import (
    DEF_INJURED,
    FREE_GK_NEW,
    FREE_MID_NEW,
    FWD_CAPTAIN,
    MID_VICE,
    NEXT_GAMEWEEK,
    load_airsenal,
)

HORIZON = [NEXT_GAMEWEEK, NEXT_GAMEWEEK + 1, NEXT_GAMEWEEK + 2]


def write_snapshot(settings, payload: dict) -> None:
    path = snapshot_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


# ------------------------------------------------------------------- parsing


def test_gameweek_keys_are_coerced_to_integers():
    """JSON has string keys; every lookup in this codebase uses an int."""
    snap = AirsenalSnapshot.model_validate(load_airsenal())
    player = snap.player(FWD_CAPTAIN)
    assert player is not None
    assert set(player.points) == {3, 4, 5, 6}
    assert player.points_in(NEXT_GAMEWEEK) == pytest.approx(6.81)


def test_unparseable_points_entries_are_dropped_not_fatal():
    raw = load_airsenal()
    raw["players"][0]["points"]["nonsense"] = "banana"
    snap = AirsenalSnapshot.model_validate(raw)
    assert snap.player(FWD_CAPTAIN) is not None


def test_total_sums_the_horizon(airsenal):
    player = airsenal.player(FWD_CAPTAIN)
    assert player.total == pytest.approx(6.81 + 5.02 + 6.11)


def test_a_player_with_no_prediction_is_absent_not_zero(airsenal):
    """A blank gameweek and a prediction of zero are different claims.

    ``get_predicted_points`` upstream defaults everyone to 0.0, which is why the
    dump script queries the prediction table directly. If that ever regressed,
    every player without a fixture would quietly read as "predicted to score
    nothing" and drag down horizon totals.
    """
    pryce = airsenal.player(620)
    assert pryce is not None
    assert NEXT_GAMEWEEK + 2 not in pryce.points


def test_unknown_element_has_no_points(airsenal):
    assert airsenal.points_for(999_999) is None
    assert airsenal.points_for(FWD_CAPTAIN, gameweek=99) is None


def test_rank_is_within_position(airsenal):
    rank = airsenal.rank_of(FWD_CAPTAIN)
    assert rank is not None
    _position_rank, pool_size = rank
    assert pool_size == len(airsenal.ranked("FWD"))
    assert airsenal.rank_of(999_999) is None


def test_ranked_all_is_every_player(airsenal):
    assert len(airsenal.ranked("all")) == len(airsenal.players)
    assert [p.total for p in airsenal.ranked()] == sorted(
        (p.total for p in airsenal.players), reverse=True
    )


# ------------------------------------------------------------------- slicing


def test_restriction_drops_gameweeks_already_played():
    """A snapshot generated before the last gameweek still carries its column.

    Summing it into a "next three gameweeks" total inflates every player who had
    a good week, and nothing downstream would notice.
    """
    snap = AirsenalSnapshot.model_validate(load_airsenal())
    assert 3 in snap.gameweeks

    sliced = snap.restricted_to(HORIZON)
    assert sliced.gameweeks == HORIZON
    assert all(3 not in player.points for player in sliced.players)
    assert snap.player(FWD_CAPTAIN).total > sliced.player(FWD_CAPTAIN).total


def test_restriction_drops_players_left_with_nothing():
    snap = AirsenalSnapshot.model_validate(load_airsenal())
    sliced = snap.restricted_to([99])
    assert sliced.players == []


def test_restriction_does_not_mutate_the_original():
    snap = AirsenalSnapshot.model_validate(load_airsenal())
    before = snap.player(FWD_CAPTAIN).total
    snap.restricted_to([NEXT_GAMEWEEK])
    assert snap.player(FWD_CAPTAIN).total == before


# ------------------------------------------------------------------- loading


def test_missing_file_is_a_note_not_an_error(settings):
    snap, note = load_snapshot(settings, gameweek=NEXT_GAMEWEEK)
    assert snap is None
    assert "not available" in note


def test_happy_path_loads_and_slices(settings):
    write_snapshot(settings, load_airsenal())
    snap, note = load_snapshot(settings, gameweek=NEXT_GAMEWEEK, horizon=3)
    assert snap is not None
    assert note == ""
    assert snap.gameweeks == HORIZON


def test_corrupt_json_is_survivable(settings):
    path = snapshot_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json")
    snap, note = load_snapshot(settings, gameweek=NEXT_GAMEWEEK)
    assert snap is None
    assert "corrupt" in note


def test_unrecognised_shape_is_survivable(settings):
    write_snapshot(settings, {"players": "not a list", "gameweeks": [NEXT_GAMEWEEK]})
    snap, note = load_snapshot(settings, gameweek=NEXT_GAMEWEEK)
    assert snap is None
    assert note


def test_a_stale_snapshot_is_refused(settings):
    write_snapshot(settings, load_airsenal(hours_old=settings.airsenal_max_age_hours + 1))
    snap, note = load_snapshot(settings, gameweek=NEXT_GAMEWEEK)
    assert snap is None
    assert "stale" in note


def test_a_snapshot_without_a_timestamp_is_refused(settings):
    raw = load_airsenal()
    raw.pop("generated_at")
    write_snapshot(settings, raw)
    snap, _note = load_snapshot(settings, gameweek=NEXT_GAMEWEEK)
    assert snap is None, "an artefact that will not say when it was made is not trustworthy"


def test_a_naive_timestamp_is_read_as_utc(settings):
    raw = load_airsenal()
    raw["generated_at"] = (
        (datetime.now(UTC) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
    )
    write_snapshot(settings, raw)
    snap, _note = load_snapshot(settings, gameweek=NEXT_GAMEWEEK)
    assert snap is not None


def test_a_snapshot_for_the_wrong_gameweek_is_refused(settings):
    """Being one gameweek behind is the dangerous case, not the missing one.

    The numbers still look entirely plausible; they are simply about last
    Saturday.
    """
    write_snapshot(settings, load_airsenal())
    snap, note = load_snapshot(settings, gameweek=NEXT_GAMEWEEK + 10)
    assert snap is None
    assert "cover" in note


def test_a_newer_schema_is_refused_rather_than_half_read(settings):
    raw = load_airsenal()
    raw["schema_version"] = SUPPORTED_SCHEMA_VERSION + 1
    write_snapshot(settings, raw)
    snap, note = load_snapshot(settings, gameweek=NEXT_GAMEWEEK)
    assert snap is None
    assert "schema" in note


def test_an_explicit_path_wins_over_the_state_dir(settings, tmp_path):
    elsewhere = tmp_path / "somewhere" / "else.json"
    settings.airsenal_snapshot_path = str(elsewhere)
    assert snapshot_path(settings) == elsewhere


# ----------------------------------------------------------------- rendering


def test_every_row_says_whether_the_player_is_owned(airsenal):
    text = airsenal.render(owned={FWD_CAPTAIN})
    rows = [line for line in text.splitlines() if line.strip().startswith(("1.", "2.", "3."))]
    assert rows
    assert all("[OWNED]" in row or "[not owned]" in row for row in rows)
    assert any(f"id={FWD_CAPTAIN} | [OWNED]" in row for row in rows)


def test_render_says_the_table_is_league_wide(airsenal):
    assert "LEAGUE-WIDE" in airsenal.render()


def test_render_carries_its_own_provenance(airsenal):
    """A projection whose age the agent cannot see is one it cannot discount."""
    text = airsenal.render()
    assert "model run" in text
    assert f"GW{NEXT_GAMEWEEK}" in text


def test_unmatched_players_are_named_and_have_no_id(airsenal):
    text = airsenal.render()
    assert "Ghost Player" in text
    assert all(player.element_id != 9999 for player in airsenal.players)


def test_transfer_plan_render_states_its_squad_source(airsenal):
    text = airsenal.transfer_plan.render()
    assert "public_api_last_published" in text
    assert f"id={FREE_MID_NEW}" in text and f"id={DEF_INJURED}" in text


def test_transfer_plan_render_uses_names_when_given_a_describer(airsenal):
    text = airsenal.transfer_plan.render(describe=lambda i: f"Player{i}")
    assert f"Player{FREE_MID_NEW}" in text


# ------------------------------------------------------------------- the brief


@pytest.fixture
def context_with_airsenal(context, airsenal, solio):
    context.airsenal = airsenal
    context.solio = solio
    return context


def test_the_squad_table_carries_both_models(context_with_airsenal):
    table = context_with_airsenal.squad_table()
    assert "| ais" in table
    assert "AIrsenal expected points" in table


def test_airsenal_value_defaults_to_the_horizon(context_with_airsenal):
    horizon = context_with_airsenal.airsenal_value(FWD_CAPTAIN)
    single = context_with_airsenal.airsenal_value(FWD_CAPTAIN, gameweek=NEXT_GAMEWEEK)
    assert horizon > single


def test_no_snapshot_means_no_value(context):
    assert context.airsenal is None
    assert context.airsenal_value(FWD_CAPTAIN) is None
    assert context.disagreement_lines() == []


def test_disagreements_are_flagged_by_name(context, solio):
    """The point of a second model is the places it differs from the first."""
    context.solio = solio
    baseline = context.projection_value(FWD_CAPTAIN)
    assert baseline is not None

    context.airsenal = AirsenalSnapshot.model_validate(
        {
            "schema_version": 1,
            "gameweeks": [NEXT_GAMEWEEK],
            "players": [
                {
                    "element_id": FWD_CAPTAIN,
                    "name": "Vasquez",
                    "position": "FWD",
                    "points": {str(NEXT_GAMEWEEK): baseline + 4.0},
                },
                {
                    "element_id": MID_VICE,
                    "name": "Hollis",
                    "position": "MID",
                    "points": {str(NEXT_GAMEWEEK): context.projection_value(MID_VICE) or 0.0},
                },
            ],
        }
    )
    lines = context.disagreement_lines(threshold=1.5)
    assert any(f"id={FWD_CAPTAIN}" in line for line in lines)
    assert any("AIrsenal is 4.00 higher" in line for line in lines)
    assert not any(f"id={MID_VICE}" in line for line in lines), "agreement is not news"


def test_the_brief_says_when_airsenal_is_missing(context):
    context.airsenal_note = "AIrsenal predictions were ignored as stale (48h old)."
    rendered = context.render()
    assert "stale" in rendered


def test_the_brief_carries_the_plan_with_its_caveat(context_with_airsenal):
    rendered = context_with_airsenal.render()
    assert "AIrsenal's own transfer plan" in rendered
    assert "published" in rendered
    assert "AIrsenal expected points" in rendered


# --------------------------------------------------------------------- tools


class _NoNetworkClient:
    """Any call here is a test failure -- these tools read the snapshot only."""

    def set_piece_notes(self) -> dict:
        raise AssertionError("the AIrsenal tools must not touch the network")

    def player_summary(self, element_id: int) -> dict:
        raise AssertionError("the AIrsenal tools must not touch the network")


def tools_for(context) -> dict:
    from fpl_buddy.agent.tools import build_tools

    return {tool.name: tool for tool in build_tools(context, _NoNetworkClient())}


def test_the_airsenal_tools_are_registered(context):
    names = set(tools_for(context))
    assert {"airsenal_points", "airsenal_top", "airsenal_transfer_plan"} <= names


def test_points_tool_reports_the_gameweek_breakdown(context_with_airsenal):
    text = tools_for(context_with_airsenal)["airsenal_points"].invoke(
        {"element_id": FWD_CAPTAIN}
    )
    assert f"GW{NEXT_GAMEWEEK} 6.81" in text
    assert "Ranked" in text


def test_points_tool_rejects_an_id_that_does_not_exist(context_with_airsenal):
    text = tools_for(context_with_airsenal)["airsenal_points"].invoke({"element_id": 999_999})
    assert "does not exist" in text


def test_points_tool_separates_no_prediction_from_no_model(context_with_airsenal):
    """A player the model skipped is not the same as the model being absent.

    Both are "no number", and an agent told the wrong one either stops trusting
    a model that is working or keeps trusting one that is not there.
    """
    text = tools_for(context_with_airsenal)["airsenal_points"].invoke(
        {"element_id": FREE_GK_NEW}
    )
    assert "no prediction" in text
    assert "not loaded" not in text


def test_top_tool_filters_by_position(context_with_airsenal):
    text = tools_for(context_with_airsenal)["airsenal_top"].invoke(
        {"position": "MID", "limit": 5}
    )
    rows = [line for line in text.splitlines() if line.strip()[:2].rstrip(".").isdigit()]
    assert rows
    assert all(", MID)" in row for row in rows)


def test_top_tool_rejects_a_nonsense_position(context_with_airsenal):
    text = tools_for(context_with_airsenal)["airsenal_top"].invoke({"position": "STRIKER"})
    assert "must be one of" in text


def test_every_airsenal_tool_explains_itself_when_there_is_no_snapshot(context):
    context.airsenal_note = "AIrsenal predictions were ignored as stale (48h old)."
    tools = tools_for(context)
    for name in ("airsenal_points", "airsenal_top", "airsenal_transfer_plan"):
        args = {"element_id": FWD_CAPTAIN} if name == "airsenal_points" else {}
        text = tools[name].invoke(args)
        assert "not loaded" in text, name
        assert "stale" in text, f"{name} should repeat the reason from the brief"


def test_transfer_plan_tool_says_when_the_optimiser_did_not_run(context_with_airsenal):
    context_with_airsenal.airsenal.transfer_plan = None
    text = tools_for(context_with_airsenal)["airsenal_transfer_plan"].invoke({})
    assert "opt-in" in text


def test_transfer_plan_tool_names_players_and_keeps_the_caveat(context_with_airsenal):
    text = tools_for(context_with_airsenal)["airsenal_transfer_plan"].invoke({})
    assert "public_api_last_published" in text
    assert "statistical model" in text
