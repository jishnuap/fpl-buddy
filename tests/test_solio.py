"""Solio parsing and the name+club join to FPL element ids.

The join is the one place a plausible-looking mistake becomes a wrong transfer,
so the fixture is built to be hostile: the same surname appears at several clubs
and twice within one club, one name is misspelled, one club code is unknown, and
one player doesn't exist at all.
"""

from __future__ import annotations

import re

from fpl_buddy.data.solio import (
    LEADERBOARD_KEYS,
    SolioSnapshot,
    join_to_elements,
    parse_snapshot,
)

from .conftest import FWD_CAPTAIN, load_json

# What the fixture rows should resolve to, generated alongside the fixture.
EXPECTED = load_json("solio-expected-join.json")


def snapshot() -> SolioSnapshot:
    return parse_snapshot(load_json("solio-latest.json"))


# ------------------------------------------------------------------- parsing


def test_metadata_is_parsed():
    snap = snapshot()
    assert snap.gameweek == 4
    assert snap.generated_at
    assert snap.source == "solio-analytics"


def test_every_board_is_picked_up():
    snap = snapshot()
    for key in LEADERBOARD_KEYS:
        assert key in snap.boards, key
    assert snap.board("topProjected")


def test_camel_case_fields_map_to_snake_case():
    row = snapshot().board("topProjected")[0]
    assert row.pr_points is not None
    assert row.captain_proj_points is not None
    assert row.opponents and row.opponents[0].opponent


def test_board_limit_is_respected():
    assert len(snapshot().board("topProjected", 2)) == 2


def test_unknown_keys_do_not_become_boards():
    snap = parse_snapshot({"gameweek": 9, "somethingNew": {"a": 1}, "count": 5})
    assert snap.boards == {}
    assert snap.gameweek == 9


def test_parser_tolerates_a_new_board_appearing():
    raw = load_json("solio-latest.json")
    raw["topSetPieces"] = raw["topProjected"][:2]
    snap = parse_snapshot(raw)
    assert "topSetPieces" in snap.boards


def test_render_is_prompt_sized_text():
    text = snapshot().render(limit=3)
    assert "Solio Analytics snapshot" in text
    assert "## topProjected" in text
    assert text.count("\n") < 200


# ------------------------------------------------------------------ ownership
#
# These boards rank the whole league. An agent captained the top row of one of
# them, which was not a player it owned, and the proposal died on validation.
# Every row now says which it is.


def player_rows(text: str) -> list[str]:
    """Just the ranked player lines -- not the legend, which mentions the markers."""
    return [line for line in text.splitlines() if re.match(r"\s+\d+\. ", line)]


def test_every_row_says_whether_the_player_is_owned(bootstrap):
    snap, _ = join_to_elements(snapshot(), bootstrap)
    rows = player_rows(snap.render(limit=5, owned={FWD_CAPTAIN}))

    assert rows
    assert all("[OWNED]" in line or "[not owned]" in line for line in rows)
    assert any(f"id={FWD_CAPTAIN} | [OWNED]" in line for line in rows)


def test_rendering_without_a_squad_marks_everything_unowned():
    rows = player_rows(snapshot().render(limit=3))
    assert rows
    assert all("[not owned]" in line for line in rows)


def test_ownership_percentage_is_not_called_own():
    """"own 74%" next to an element id reads as "you own him". It is not that."""
    rows = player_rows(snapshot().render(limit=3))
    assert any("sel " in line for line in rows)
    assert not any("own " in line.replace("[not owned]", "") for line in rows)


def test_the_legend_says_the_boards_are_league_wide():
    text = snapshot().render(limit=1)
    assert "LEAGUE-WIDE" in text


# ---------------------------------------------------------------------- join


def test_join_resolves_every_matchable_row(bootstrap):
    snap, unmatched = join_to_elements(snapshot(), bootstrap)
    resolved = {
        (row.name, row.team): row.element_id
        for rows in snap.boards.values()
        for row in rows
    }
    for expected in EXPECTED:
        key = (expected["name"], expected["team"])
        assert resolved[key] == expected["element_id"], key
    assert len(unmatched) == sum(1 for e in EXPECTED if e["element_id"] is None)


def test_exact_name_within_the_club_wins(bootstrap):
    snap, _ = join_to_elements(snapshot(), bootstrap)
    row = next(r for r in snap.board("topProjected") if r.name == "Vasquez")
    assert row.element_id == FWD_CAPTAIN


def test_same_surname_at_two_clubs_is_not_confused(bootstrap):
    """'Abbott' exists at ARS and at NEW. Club first, then name."""
    snap, _ = join_to_elements(snapshot(), bootstrap)
    abbotts = {
        (r.team, r.position): r.element_id
        for r in snap.board("topProjected")
        if r.name == "Abbott"
    }
    assert abbotts[("ARS", "GKP")] == 110
    assert abbotts[("NEW", "MID")] == 630
    assert len(set(abbotts.values())) == len(abbotts), "distinct rows, distinct ids"


def test_misspelled_name_still_matches_within_the_club(bootstrap):
    snap, _ = join_to_elements(snapshot(), bootstrap)
    row = next(r for r in snap.board("topProjected") if r.name == "Kelsal")
    assert row.element_id == 433


def test_nonexistent_player_is_reported_not_guessed(bootstrap):
    snap, unmatched = join_to_elements(snapshot(), bootstrap)
    row = next(r for r in snap.board("topProjected") if r.name == "Zoltan Nevermatch")
    assert row.element_id is None
    assert any("Zoltan Nevermatch" in entry for entry in unmatched)


def test_unknown_club_code_is_never_matched_globally(bootstrap):
    """A club code FPL doesn't use must not fall back to a league-wide search.

    Surnames repeat across clubs, so a global fuzzy match here would return a
    real id for the wrong player -- the exact failure that costs you a transfer.
    """
    snap, unmatched = join_to_elements(snapshot(), bootstrap)
    row = next(r for r in snap.board("topProjected") if r.team == "XYZ")
    assert row.element_id is None
    assert any("XYZ" in entry for entry in unmatched)


def test_a_high_threshold_rejects_weak_matches(bootstrap):
    snap, unmatched = join_to_elements(snapshot(), bootstrap, min_score=100)
    row = next(r for r in snap.board("topProjected") if r.name == "Kelsal")
    assert row.element_id is None, "a misspelling must fail at threshold 100"
    assert any("Kelsal" in entry for entry in unmatched)


def test_unmatched_list_is_deduplicated_and_sorted(bootstrap):
    _snap, unmatched = join_to_elements(snapshot(), bootstrap)
    assert unmatched == sorted(set(unmatched))


def test_projection_lookup_by_element_id(bootstrap):
    snap, _ = join_to_elements(snapshot(), bootstrap)
    row = snap.projection_for(FWD_CAPTAIN)
    assert row is not None and row.pr_points is not None
    assert snap.projection_for(999_999) is None


def test_summary_includes_the_id_once_joined(bootstrap):
    snap, _ = join_to_elements(snapshot(), bootstrap)
    row = snap.projection_for(FWD_CAPTAIN)
    assert f"id={FWD_CAPTAIN}" in row.summary()


def test_join_is_cached_per_name_club_position(bootstrap):
    """The same player appears on several boards; every copy gets the same id."""
    snap, _ = join_to_elements(snapshot(), bootstrap)
    ids = {
        row.element_id
        for board in ("topProjected", "topCaptains", "topGoals")
        for row in snap.board(board)
        if row.name == "Vasquez"
    }
    assert ids == {FWD_CAPTAIN}
