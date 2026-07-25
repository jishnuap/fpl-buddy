"""Every guardrail in decisions/validate.py, in both directions.

This code can spend real points, so each check gets a test that proves it fires
*and* the clean case proves it doesn't fire spuriously. The fixture squad is a
legal 15 (2/5/5/3, three per club across five clubs, 1-3-4-3 XI), so anything
these tests flag is caused by the one field the test perturbed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl_buddy.decisions.validate import (
    EXECUTION_CUTOFF,
    build_picks_payload,
    resolved_squad_ids,
    validate,
)
from fpl_buddy.fpl.models import Pick

from .conftest import (
    DEF_ARS,
    DEF_BENCH_CHE,
    DEF_BENCH_TOT,
    DEF_INJURED,
    FREE_DEF_NEW,
    FREE_FWD_DOUBTFUL,
    FREE_FWD_EXPENSIVE,
    FREE_FWD_UNAVAILABLE,
    FREE_GK_NEW,
    FREE_MID_ARS,
    FREE_MID_NEW,
    FWD_CAPTAIN,
    FWD_LIV,
    FWD_TOT,
    GK_RESERVE,
    GK_STARTER,
    MID_BENCH,
    MID_LIV,
    MID_VICE,
    NEXT_GAMEWEEK,
    codes,
    fatal_codes,
    make_proposal,
    make_transfer,
)

CURRENT_XI = [
    GK_STARTER, DEF_ARS, 220, DEF_INJURED, MID_VICE, 230, MID_LIV, 430,
    FWD_CAPTAIN, FWD_LIV, FWD_TOT,
]
CURRENT_BENCH = [GK_RESERVE, DEF_BENCH_TOT, DEF_BENCH_CHE, MID_BENCH]

# A second legal swap, used to push the transfer count past the free transfer.
SECOND_SWAP = (DEF_INJURED, FREE_DEF_NEW)


# --------------------------------------------------------------- the clean case


def test_rolling_proposal_is_clean(context, settings):
    assert validate(make_proposal(), context, settings) == []


def test_clean_proposal_stays_clean_at_execution_time(context, settings):
    assert validate(make_proposal(), context, settings, for_execution=True) == []


def test_single_legal_transfer_is_clean(context, settings):
    proposal = make_proposal(transfers=[make_transfer(MID_LIV, FREE_MID_NEW)])
    assert validate(proposal, context, settings) == []


def test_explicit_legal_lineup_is_clean(context, settings):
    proposal = make_proposal(starting_xi=CURRENT_XI, bench_order=CURRENT_BENCH)
    assert validate(proposal, context, settings) == []


# ------------------------------------------------------------------- gameweek


def test_wrong_gameweek_is_fatal(context, settings):
    issues = validate(make_proposal(gameweek=99), context, settings)
    assert "wrong_gameweek" in fatal_codes(issues)


# ------------------------------------------------------------------- deadline


def test_execution_inside_cutoff_is_refused(context, settings):
    context.gameweek.deadline_time = datetime.now(UTC) + EXECUTION_CUTOFF / 2
    issues = validate(make_proposal(), context, settings, for_execution=True)
    assert "past_deadline" in fatal_codes(issues)


def test_past_deadline_is_refused(context, settings):
    context.gameweek.deadline_time = datetime.now(UTC) - timedelta(hours=1)
    issues = validate(make_proposal(), context, settings, for_execution=True)
    assert "past_deadline" in fatal_codes(issues)


def test_deadline_is_not_checked_when_only_proposing(context, settings):
    """A proposal built after the deadline is still worth showing; just not POSTing."""
    context.gameweek.deadline_time = datetime.now(UTC) - timedelta(hours=1)
    issues = validate(make_proposal(), context, settings, for_execution=False)
    assert "past_deadline" not in codes(issues)


def test_deadline_just_outside_cutoff_is_allowed(context, settings):
    context.gameweek.deadline_time = datetime.now(UTC) + EXECUTION_CUTOFF + timedelta(seconds=30)
    issues = validate(make_proposal(), context, settings, for_execution=True)
    assert "past_deadline" not in codes(issues)


# ----------------------------------------------------------------------- chips


def test_unknown_chip_is_fatal(context, settings):
    issues = validate(make_proposal(chip="turbo_boost"), context, settings)
    assert "unknown_chip" in fatal_codes(issues)


def test_already_played_chip_is_fatal(context, settings):
    # bboost is "played" in the fixture.
    issues = validate(make_proposal(chip="bboost"), context, settings)
    assert "chip_unavailable" in fatal_codes(issues)


def test_available_chip_is_accepted(context, settings):
    issues = validate(make_proposal(chip="3xc"), context, settings)
    assert codes(issues) == set()


def test_chip_conflicts_with_one_already_active(context, settings):
    context.my_team.active_chip = "3xc"
    issues = validate(make_proposal(chip="wildcard"), context, settings)
    assert "chip_conflict" in fatal_codes(issues)


# ------------------------------------------------------------------- transfers


def test_self_transfer_is_fatal(context, settings):
    proposal = make_proposal(transfers=[make_transfer(MID_LIV, MID_LIV)])
    assert "self_transfer" in fatal_codes(validate(proposal, context, settings))


def test_cannot_sell_a_player_you_do_not_own(context, settings):
    proposal = make_proposal(transfers=[make_transfer(FREE_MID_NEW, MID_LIV)])
    assert "not_in_squad" in fatal_codes(validate(proposal, context, settings))


def test_unknown_incoming_element_is_fatal(context, settings):
    proposal = make_proposal(transfers=[make_transfer(MID_LIV, 999_999)])
    assert "unknown_target" in fatal_codes(validate(proposal, context, settings))


def test_cannot_buy_a_player_you_already_own(context, settings):
    proposal = make_proposal(transfers=[make_transfer(MID_LIV, FWD_LIV)])
    assert "already_owned" in fatal_codes(validate(proposal, context, settings))


def test_selling_the_same_player_twice_is_fatal(context, settings):
    proposal = make_proposal(
        transfers=[
            make_transfer(MID_LIV, FREE_MID_NEW),
            make_transfer(MID_LIV, 631),
        ]
    )
    assert "duplicate_out" in fatal_codes(validate(proposal, context, settings))


def test_buying_the_same_player_twice_is_fatal(context, settings):
    proposal = make_proposal(
        transfers=[
            make_transfer(MID_LIV, FREE_MID_NEW),
            make_transfer(430, FREE_MID_NEW),
        ]
    )
    assert "duplicate_in" in fatal_codes(validate(proposal, context, settings))


def test_a_malformed_transfer_stops_structural_checks(context, settings):
    """Once the squad can't be resolved, later checks would be noise."""
    proposal = make_proposal(
        transfers=[make_transfer(MID_LIV, 999_999)],
        captaincy=make_proposal().captaincy,
        starting_xi=[1, 2, 3],  # nonsense that would normally trip xi_size
    )
    assert codes(validate(proposal, context, settings)) == {"unknown_target"}


# -------------------------------------------------------------------- pricing


def test_validator_overwrites_prices_the_model_guessed(context, settings):
    """MID_LIV's selling price is below now_cost -- the sell-on rule. Never guess."""
    move = make_transfer(MID_LIV, FREE_MID_NEW, selling_price=999, purchase_price=1)
    assert validate(make_proposal(transfers=[move]), context, settings) == []

    pick = context.my_team.pick_for(MID_LIV)
    target = context.bootstrap.player(FREE_MID_NEW)
    assert pick.selling_price != target.now_cost, "fixture should exercise the sell-on rule"
    assert move.selling_price == pick.selling_price
    assert move.purchase_price == target.now_cost


def test_selling_price_comes_from_the_pick_not_now_cost(context, settings):
    pick = context.my_team.pick_for(MID_LIV)
    player = context.bootstrap.player(MID_LIV)
    assert pick.selling_price < player.now_cost, "fixture precondition"

    move = make_transfer(MID_LIV, FREE_MID_NEW)
    validate(make_proposal(transfers=[move]), context, settings)
    assert move.selling_price == pick.selling_price != player.now_cost


# --------------------------------------------------------------------- budget


def test_over_budget_is_fatal(context, settings):
    proposal = make_proposal(transfers=[make_transfer(FWD_LIV, FREE_FWD_EXPENSIVE)])
    issues = validate(proposal, context, settings)
    assert "over_budget" in fatal_codes(issues)


def test_spending_exactly_the_bank_is_allowed(context, settings):
    """Boundary: remaining == 0 must pass, not fail."""
    pick = context.my_team.pick_for(FWD_LIV)
    target = context.bootstrap.player(FREE_FWD_DOUBTFUL)
    context.my_team.bank = target.now_cost - pick.selling_price
    assert context.my_team.bank > 0, "fixture precondition"

    proposal = make_proposal(transfers=[make_transfer(FWD_LIV, FREE_FWD_DOUBTFUL)])
    issues = validate(proposal, context, settings)
    assert "over_budget" not in codes(issues)


def test_one_pence_short_is_fatal(context, settings):
    pick = context.my_team.pick_for(FWD_LIV)
    target = context.bootstrap.player(FREE_FWD_DOUBTFUL)
    context.my_team.bank = target.now_cost - pick.selling_price - 1

    proposal = make_proposal(transfers=[make_transfer(FWD_LIV, FREE_FWD_DOUBTFUL)])
    assert "over_budget" in fatal_codes(validate(proposal, context, settings))


# ----------------------------------------------------------------------- hits


def test_free_transfer_costs_nothing(context, settings):
    proposal = make_proposal(transfers=[make_transfer(MID_LIV, FREE_MID_NEW)], points_hit=0)
    assert codes(validate(proposal, context, settings)) == set()


def test_hit_mismatch_is_corrected_and_not_fatal(context, settings):
    proposal = make_proposal(transfers=[make_transfer(MID_LIV, FREE_MID_NEW)], points_hit=4)
    issues = validate(proposal, context, settings)
    assert "hit_mismatch" in codes(issues)
    assert fatal_codes(issues) == set()
    assert proposal.points_hit == 0, "validator must correct the claimed hit"


def test_understated_hit_is_corrected_upwards(context, settings):
    settings.max_points_hit = 4
    proposal = make_proposal(
        transfers=[make_transfer(MID_LIV, FREE_MID_NEW), make_transfer(*SECOND_SWAP)],
        points_hit=0,
    )
    issues = validate(proposal, context, settings)
    assert "hit_mismatch" in codes(issues)
    assert proposal.points_hit == 4


def test_hit_beyond_the_ceiling_is_fatal(context, settings):
    assert settings.max_points_hit == 0, "default must be zero"
    proposal = make_proposal(
        transfers=[make_transfer(MID_LIV, FREE_MID_NEW), make_transfer(*SECOND_SWAP)],
        points_hit=4,
    )
    assert "hit_exceeds_limit" in fatal_codes(validate(proposal, context, settings))


def test_hit_within_a_raised_ceiling_is_allowed(context, settings):
    settings.max_points_hit = 4
    proposal = make_proposal(
        transfers=[make_transfer(MID_LIV, FREE_MID_NEW), make_transfer(*SECOND_SWAP)],
        points_hit=4,
    )
    assert fatal_codes(validate(proposal, context, settings)) == set()


def test_wildcard_makes_transfers_free(context, settings):
    proposal = make_proposal(
        chip="wildcard",
        transfers=[
            make_transfer(MID_LIV, FREE_MID_NEW),
            make_transfer(*SECOND_SWAP),
            make_transfer(GK_STARTER, FREE_GK_NEW),
        ],
        points_hit=0,
    )
    assert fatal_codes(validate(proposal, context, settings)) == set()


def test_unlimited_free_transfers_cost_nothing(context, settings):
    """`transfers.limit` is None on a wildcard/pre-season; the parser maps it high."""
    context.my_team.free_transfers = 15
    proposal = make_proposal(
        transfers=[make_transfer(MID_LIV, FREE_MID_NEW), make_transfer(*SECOND_SWAP)],
        points_hit=0,
    )
    assert fatal_codes(validate(proposal, context, settings)) == set()


# ------------------------------------------------------------- squad legality


def test_transfer_that_breaks_the_position_shape_is_fatal(context, settings):
    """A midfielder out and a defender in leaves 4 MID / 6 DEF."""
    proposal = make_proposal(transfers=[make_transfer(MID_LIV, FREE_DEF_NEW)])
    issues = validate(proposal, context, settings)
    assert "illegal_squad_shape" in fatal_codes(issues)


def test_fourth_player_from_one_club_is_fatal(context, settings):
    proposal = make_proposal(transfers=[make_transfer(MID_LIV, FREE_MID_ARS)])
    issues = validate(proposal, context, settings)
    assert "club_limit" in fatal_codes(issues)


def test_three_from_one_club_is_allowed(context, settings):
    """Boundary: the fixture squad already sits on the limit and must pass."""
    assert validate(make_proposal(), context, settings) == []


def test_short_squad_is_fatal(context, settings):
    context.my_team.picks = context.my_team.picks[:-1]
    issues = validate(make_proposal(), context, settings)
    assert "squad_size" in fatal_codes(issues)


def test_element_missing_from_bootstrap_is_fatal(context, settings):
    context.my_team.picks[-1] = Pick(
        element=999_999, position=15, selling_price=50, purchase_price=50
    )
    issues = validate(make_proposal(), context, settings)
    assert "unknown_player" in fatal_codes(issues)


# ------------------------------------------------------------------ captaincy


def test_captain_cannot_also_be_vice(context, settings):
    proposal = make_proposal(
        captaincy=make_proposal().captaincy.model_copy(
            update={"vice_captain_id": FWD_CAPTAIN}
        )
    )
    assert "captain_equals_vice" in fatal_codes(validate(proposal, context, settings))


def test_captain_outside_the_squad_is_fatal(context, settings):
    proposal = make_proposal(
        captaincy=make_proposal().captaincy.model_copy(update={"captain_id": FREE_MID_NEW})
    )
    assert "captain_not_in_squad" in fatal_codes(validate(proposal, context, settings))


def test_captaining_a_player_you_are_selling_is_fatal(context, settings):
    proposal = make_proposal(transfers=[make_transfer(FWD_CAPTAIN, 241)])
    issues = validate(proposal, context, settings)
    assert "captain_not_in_squad" in fatal_codes(issues)


def test_vice_outside_the_squad_is_fatal(context, settings):
    proposal = make_proposal(
        captaincy=make_proposal().captaincy.model_copy(update={"vice_captain_id": FREE_MID_NEW})
    )
    issues = validate(proposal, context, settings)
    assert "captain_not_in_squad" in fatal_codes(issues)


def test_flagged_captain_warns_without_blocking(context, settings):
    """An injured captain is a judgement call, not an illegal move."""
    proposal = make_proposal(
        captaincy=make_proposal().captaincy.model_copy(update={"captain_id": DEF_INJURED})
    )
    issues = validate(proposal, context, settings)
    assert "captain_flagged" in codes(issues)
    assert fatal_codes(issues) == set()


# ----------------------------------------------------------- transfer targets


def test_unavailable_target_is_fatal(context, settings):
    proposal = make_proposal(transfers=[make_transfer(FWD_LIV, FREE_FWD_UNAVAILABLE)])
    issues = validate(proposal, context, settings)
    assert "target_unavailable" in fatal_codes(issues)


def test_doubtful_target_warns_without_blocking(context, settings):
    proposal = make_proposal(transfers=[make_transfer(FWD_LIV, FREE_FWD_DOUBTFUL)])
    issues = validate(proposal, context, settings)
    assert "target_flagged" in codes(issues)
    assert fatal_codes(issues) == set()


# ------------------------------------------------------------- XI and bench


def test_empty_lineup_leaves_the_team_alone(context, settings):
    assert validate(make_proposal(starting_xi=[], bench_order=[]), context, settings) == []


def test_wrong_xi_size_is_fatal(context, settings):
    proposal = make_proposal(starting_xi=CURRENT_XI[:10], bench_order=CURRENT_BENCH)
    issues = validate(proposal, context, settings)
    assert "xi_size" in fatal_codes(issues)


def test_wrong_bench_size_is_fatal(context, settings):
    proposal = make_proposal(starting_xi=CURRENT_XI, bench_order=CURRENT_BENCH[:3])
    issues = validate(proposal, context, settings)
    assert "bench_size" in fatal_codes(issues)


def test_duplicate_in_lineup_is_fatal(context, settings):
    xi = list(CURRENT_XI)
    xi[0] = FWD_CAPTAIN  # now appears twice
    proposal = make_proposal(starting_xi=xi, bench_order=CURRENT_BENCH)
    issues = validate(proposal, context, settings)
    assert "lineup_duplicates" in fatal_codes(issues)


def test_lineup_containing_an_unowned_player_is_fatal(context, settings):
    xi = list(CURRENT_XI)
    xi[6] = FREE_MID_NEW
    proposal = make_proposal(starting_xi=xi, bench_order=CURRENT_BENCH)
    issues = validate(proposal, context, settings)
    assert "lineup_mismatch" in fatal_codes(issues)


def test_lineup_must_reflect_the_post_transfer_squad(context, settings):
    """The XI has to name the incoming player, not the one being sold."""
    proposal = make_proposal(
        transfers=[make_transfer(MID_LIV, FREE_MID_NEW)],
        starting_xi=CURRENT_XI,
        bench_order=CURRENT_BENCH,
    )
    issues = validate(proposal, context, settings)
    assert "lineup_mismatch" in fatal_codes(issues)


def test_lineup_that_does_reflect_the_transfer_is_clean(context, settings):
    xi = [FREE_MID_NEW if e == MID_LIV else e for e in CURRENT_XI]
    proposal = make_proposal(
        transfers=[make_transfer(MID_LIV, FREE_MID_NEW)],
        starting_xi=xi,
        bench_order=CURRENT_BENCH,
    )
    assert validate(proposal, context, settings) == []


def test_illegal_formation_is_fatal(context, settings):
    """Two keepers in the XI, and only two defenders left behind them."""
    xi = list(CURRENT_XI)
    xi[1] = GK_RESERVE
    bench = [DEF_ARS, DEF_BENCH_TOT, DEF_BENCH_CHE, MID_BENCH]
    proposal = make_proposal(starting_xi=xi, bench_order=bench)
    issues = validate(proposal, context, settings)
    assert "illegal_formation" in fatal_codes(issues)


@pytest.mark.parametrize(
    ("xi", "expected"),
    [
        # 1-3-4-3 and 1-4-4-2 are legal; 1-2-4-4 is not (only 2 DEF, 4 FWD max 3).
        (
            [GK_STARTER, DEF_ARS, 220, DEF_INJURED, DEF_BENCH_TOT, MID_VICE, 230, MID_LIV,
             430, FWD_CAPTAIN, FWD_LIV],
            set(),
        ),
        (
            [GK_STARTER, DEF_ARS, 220, MID_VICE, 230, MID_LIV, 430, MID_BENCH,
             FWD_CAPTAIN, FWD_LIV, FWD_TOT],
            {"illegal_formation"},
        ),
    ],
)
def test_formation_bounds(context, settings, xi, expected):
    bench = [e for e in CURRENT_XI + CURRENT_BENCH if e not in xi]
    bench.sort(key=lambda e: context.bootstrap.player(e).element_type)
    proposal = make_proposal(starting_xi=xi, bench_order=bench)
    issues = validate(proposal, context, settings)
    assert {c for c in codes(issues) if c == "illegal_formation"} == expected


def test_reserve_keeper_must_be_bench_slot_twelve(context, settings):
    bench = [DEF_BENCH_TOT, GK_RESERVE, DEF_BENCH_CHE, MID_BENCH]
    proposal = make_proposal(starting_xi=CURRENT_XI, bench_order=bench)
    issues = validate(proposal, context, settings)
    assert "bench_keeper_order" in codes(issues)
    assert fatal_codes(issues) == set(), "wrong bench order is recoverable, not illegal"


def test_benching_the_captain_is_fatal(context, settings):
    proposal = make_proposal(
        captaincy=make_proposal().captaincy.model_copy(update={"captain_id": MID_BENCH}),
        starting_xi=CURRENT_XI,
        bench_order=CURRENT_BENCH,
    )
    issues = validate(proposal, context, settings)
    assert "captain_benched" in fatal_codes(issues)


# ------------------------------------------------------- payload construction


def test_resolved_squad_with_no_transfers_is_the_current_squad(context):
    squad = resolved_squad_ids(make_proposal(), context)
    assert sorted(squad) == sorted(p.element for p in context.my_team.picks)


def test_resolved_squad_applies_transfers(context):
    proposal = make_proposal(transfers=[make_transfer(MID_LIV, FREE_MID_NEW)])
    squad = resolved_squad_ids(proposal, context)
    assert MID_LIV not in squad
    assert FREE_MID_NEW in squad
    assert len(squad) == 15


def test_picks_payload_follows_the_explicit_lineup(context):
    proposal = make_proposal(starting_xi=CURRENT_XI, bench_order=CURRENT_BENCH)
    squad = resolved_squad_ids(proposal, context)
    payload = build_picks_payload(proposal, context, squad)

    assert [p["position"] for p in payload] == list(range(1, 16))
    assert [p["element"] for p in payload] == CURRENT_XI + CURRENT_BENCH
    assert [p["element"] for p in payload if p["is_captain"]] == [FWD_CAPTAIN]
    assert [p["element"] for p in payload if p["is_vice_captain"]] == [MID_VICE]
    assert payload[11]["element"] == GK_RESERVE, "slot 12 must be the reserve keeper"


def test_picks_payload_preserves_order_when_the_agent_left_the_lineup_alone(context):
    proposal = make_proposal()
    payload = build_picks_payload(proposal, context, resolved_squad_ids(proposal, context))
    current = [p.element for p in sorted(context.my_team.picks, key=lambda p: p.position)]
    assert [p["element"] for p in payload] == current


def test_picks_payload_splices_an_incoming_player_into_the_slot_it_vacated(context):
    proposal = make_proposal(transfers=[make_transfer(MID_LIV, FREE_MID_NEW)])
    payload = build_picks_payload(proposal, context, resolved_squad_ids(proposal, context))

    slot = next(p.position for p in context.my_team.picks if p.element == MID_LIV)
    assert payload[slot - 1]["element"] == FREE_MID_NEW
    assert MID_LIV not in [p["element"] for p in payload]
    assert len(payload) == 15


def test_picks_payload_refuses_a_lineup_that_does_not_match_the_squad(context):
    proposal = make_proposal(
        transfers=[make_transfer(MID_LIV, FREE_MID_NEW)],
        starting_xi=CURRENT_XI,  # still names the player being sold
        bench_order=CURRENT_BENCH,
    )
    squad = resolved_squad_ids(proposal, context)
    with pytest.raises(ValueError, match="does not match the resulting squad"):
        build_picks_payload(proposal, context, squad)


def test_picks_payload_marks_exactly_one_captain_and_one_vice(context):
    proposal = make_proposal(starting_xi=CURRENT_XI, bench_order=CURRENT_BENCH)
    payload = build_picks_payload(proposal, context, resolved_squad_ids(proposal, context))
    assert sum(p["is_captain"] for p in payload) == 1
    assert sum(p["is_vice_captain"] for p in payload) == 1


def test_every_payload_slot_carries_the_four_required_keys(context):
    proposal = make_proposal()
    payload = build_picks_payload(proposal, context, resolved_squad_ids(proposal, context))
    for slot in payload:
        assert set(slot) == {"element", "position", "is_captain", "is_vice_captain"}


# ------------------------------------------------------------------ regression


def test_validate_reports_every_problem_not_just_the_first(context, settings):
    """The notification is only useful if it lists all the reasons."""
    proposal = make_proposal(
        gameweek=99,
        chip="bboost",
        captaincy=make_proposal().captaincy.model_copy(update={"captain_id": FREE_MID_NEW}),
    )
    found = codes(validate(proposal, context, settings))
    assert {"wrong_gameweek", "chip_unavailable", "captain_not_in_squad"} <= found


def test_validate_does_not_mutate_the_squad(context, settings):
    before = [(p.element, p.position) for p in context.my_team.picks]
    validate(make_proposal(transfers=[make_transfer(MID_LIV, FREE_MID_NEW)]), context, settings)
    assert [(p.element, p.position) for p in context.my_team.picks] == before


def test_gameweek_id_matches_the_fixture(context):
    assert context.gameweek.id == NEXT_GAMEWEEK
