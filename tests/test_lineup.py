"""The starting XI as a reviewable decision.

The XI and bench order were always submitted to FPL, always validated, and
never shown to anyone. The agent filled them in to satisfy a constraint and
they reached the executor unread, so "the lineup did not change" and "nobody
looked at the lineup" were indistinguishable from the outside.

These tests are mostly about that distinction: the diff has to be honest about
transfers, and an unchanged lineup has to say so out loud rather than render as
nothing at all.
"""

from __future__ import annotations

from fpl_buddy.notify import lineup_lines, render_proposal

from .conftest import (
    DEF_BENCH_CHE,
    DEF_BENCH_TOT,
    DEF_INJURED,
    FREE_DEF_NEW,
    FWD_CAPTAIN,
    GK_RESERVE,
    GK_STARTER,
    MID_BENCH,
    MID_LIV,
    MID_VICE,
    make_proposal,
    make_stored,
    make_transfer,
)

CURRENT_XI = [
    GK_STARTER, 210, 220, DEF_INJURED, MID_VICE, 230, MID_LIV, 430,
    FWD_CAPTAIN, 340, 440,
]
CURRENT_BENCH = [GK_RESERVE, DEF_BENCH_TOT, DEF_BENCH_CHE, MID_BENCH]

NAMES = {
    DEF_INJURED: "Injured", DEF_BENCH_TOT: "BenchDef", DEF_BENCH_CHE: "OtherDef",
    MID_BENCH: "BenchMid", FREE_DEF_NEW: "NewDef", GK_STARTER: "Keeper",
}


def stored(context, agent=None, *, xi=None, bench=None, **overrides):
    return make_stored(
        agent or make_proposal(starting_xi=xi or CURRENT_XI, bench_order=bench or CURRENT_BENCH),
        context,
        previous_starting_xi=CURRENT_XI,
        previous_bench_order=CURRENT_BENCH,
        squad_names=NAMES,
        **overrides,
    )


# --------------------------------------------------------------------- the diff


def test_an_unchanged_lineup_says_so_rather_than_rendering_nothing(context):
    """Silence here is not the same claim as "unchanged", and only one is safe."""
    lines = lineup_lines(stored(context))
    assert lines == ["Line-up:      unchanged"]


def test_a_swap_names_both_sides(context):
    xi = [e for e in CURRENT_XI if e != DEF_INJURED] + [DEF_BENCH_TOT]
    bench = [GK_RESERVE, DEF_INJURED, DEF_BENCH_CHE, MID_BENCH]
    lines = lineup_lines(stored(context, xi=xi, bench=bench))
    assert "start BenchDef" in lines[0]
    assert "bench Injured" in lines[0]


def test_transfers_are_not_reported_as_lineup_changes(context):
    """A bought player starting is the transfer you already read about.

    Listing it as a line-up change buries the decision that matters -- a fit
    player being dropped -- under something the reader has already been told.
    """
    agent = make_proposal(
        transfers=[make_transfer(DEF_INJURED, FREE_DEF_NEW)],
        starting_xi=[e for e in CURRENT_XI if e != DEF_INJURED] + [FREE_DEF_NEW],
        bench_order=CURRENT_BENCH,
    )
    assert lineup_lines(stored(context, agent)) == ["Line-up:      unchanged"]


def test_a_bench_reorder_alone_is_still_reported(context):
    """Order is the whole point of a bench: it is the auto-sub priority."""
    reordered = [GK_RESERVE, MID_BENCH, DEF_BENCH_TOT, DEF_BENCH_CHE]
    lines = lineup_lines(stored(context, bench=reordered))
    assert lines == ["Line-up:      XI unchanged, bench order changed"]


def test_no_previous_lineup_means_no_claim_either_way(context):
    """Proposals stored before this existed cannot be diffed, so they say nothing."""
    proposal = make_stored(
        make_proposal(starting_xi=CURRENT_XI, bench_order=CURRENT_BENCH), context
    )
    assert lineup_lines(proposal) == []


# ------------------------------------------------------------------- rendering


def test_the_rendered_proposal_carries_the_lineup_and_its_reason(context, settings):
    agent = make_proposal(
        starting_xi=[e for e in CURRENT_XI if e != DEF_INJURED] + [DEF_BENCH_TOT],
        bench_order=[GK_RESERVE, DEF_INJURED, DEF_BENCH_CHE, MID_BENCH],
        lineup_reason="Injured is a doubt; BenchDef has a home fixture and starts every week.",
    )
    _subject, text, _html = render_proposal(stored(context, agent), settings)
    assert "Line-up:" in text
    assert "BenchDef" in text
    assert "Injured is a doubt" in text


def test_an_old_proposal_without_the_new_fields_still_loads(context, settings):
    """Stored proposals predate these fields; rendering one must not blow up."""
    proposal = make_stored(make_proposal(), context)
    assert proposal.previous_starting_xi == []
    assert proposal.agent.lineup_reason == ""
    _subject, text, _html = render_proposal(proposal, settings)
    assert "Captain:" in text


# ------------------------------------------------- what the agent gets to decide on


def test_a_stronger_bench_player_is_surfaced(context, solio):
    """Without this the agent had no evidence a swap was even available."""
    context.solio = solio
    lines = context.bench_challenger_lines()
    assert lines, "the fixture squad has a flagged starter with bench cover"
    assert all(" in / " in line for line in lines), "a swap the agent cannot address is no use"


def test_a_flagged_starter_is_surfaced_whatever_the_projection_says(context, solio):
    """A projection has not priced in an injury announced this morning."""
    context.solio = solio
    lines = context.bench_challenger_lines()
    assert any("FLAGGED" in line for line in lines)


def test_the_reserve_keeper_is_left_out_of_it(context, solio):
    """Swapping keepers is a real call but never a marginal one."""
    context.solio = solio
    assert not any(f"ids {GK_RESERVE} " in line for line in context.bench_challenger_lines())


def test_a_flagged_starter_surfaces_even_with_no_projections_at_all(context):
    """Solio ships leaderboards, so most of a squad has no row -- bench players
    especially, being on the bench for a reason. If this needed a Solio
    projection it would almost never fire, which is the same as not existing.
    FPL's own ep_next carries it, and an injury does not wait for a projection.
    """
    context.solio = None
    lines = context.bench_challenger_lines()
    assert any("FLAGGED" in line for line in lines)
    assert all(" ep " in line for line in lines), "no Solio row means the ep fallback is in use"


def test_a_marginal_gap_is_not_worth_reporting(context, solio):
    """Churning the XI over a tenth of a point costs the bench cover it buys."""
    context.solio = solio
    assert context.bench_challenger_lines(margin=100.0) == [
        line for line in context.bench_challenger_lines(margin=100.0) if "FLAGGED" in line
    ], "only the flagged case should survive an impossibly high margin"
