"""Detecting that the team news moved after the proposal was written.

The propose job now runs an hour before the deadline, which is late enough to
catch press conferences but still early enough that a player can be ruled out
in between. These tests pin down what counts as "re-decide" and what doesn't --
the distinction matters, because every false positive is a wasted agent run in
the last few minutes before a deadline.
"""

from __future__ import annotations

from fpl_buddy.decisions.schema import AgentProposal
from fpl_buddy.decisions.staleness import material_changes, rethink_instruction

from .conftest import FREE_MID_NEW, FWD_CAPTAIN, MID_LIV, MID_VICE
from .fakes import ONE_TRANSFER_PROPOSAL, ROLLING_PROPOSAL


def rolling() -> AgentProposal:
    return AgentProposal.model_validate(ROLLING_PROPOSAL)


def transferring() -> AgentProposal:
    return AgentProposal.model_validate(ONE_TRANSFER_PROPOSAL)


def injure(context, element_id: int, *, status: str = "i", chance: int | None = 25) -> None:
    player = context.bootstrap.player(element_id)
    assert player is not None
    player.status = status
    player.chance_of_playing_next_round = chance
    player.news = "Knock picked up in training."


# ------------------------------------------------------------- nothing moved


def test_an_untouched_world_needs_no_rethink(context):
    assert material_changes(rolling(), context) == []


def test_a_healthy_transfer_target_is_not_a_change(context):
    assert material_changes(transferring(), context) == []


# ----------------------------------------------------------------- the armband


def test_a_flagged_captain_triggers_a_rethink(context):
    injure(context, FWD_CAPTAIN)
    reasons = material_changes(rolling(), context)

    assert len(reasons) == 1
    assert "captain" in reasons[0]
    assert "Knock picked up in training." in reasons[0]


def test_a_flagged_vice_triggers_a_rethink(context):
    """The vice is the whole point of a vice; a doubtful one is not cover."""
    injure(context, MID_VICE)
    assert "vice-captain" in material_changes(rolling(), context)[0]


def test_a_mere_doubt_is_enough_for_the_armband(context):
    """75% is the guardrails' line, and captaincy is the highest-variance call."""
    injure(context, FWD_CAPTAIN, status="a", chance=50)
    assert material_changes(rolling(), context) != []


def test_a_fully_fit_captain_with_a_chance_of_100_is_fine(context):
    injure(context, FWD_CAPTAIN, status="a", chance=100)
    assert material_changes(rolling(), context) == []


# ------------------------------------------------------------------ transfers


def test_an_unavailable_transfer_target_triggers_a_rethink(context):
    injure(context, FREE_MID_NEW, status="u", chance=0)
    reasons = material_changes(transferring(), context)

    assert len(reasons) == 1
    assert "will not play" in reasons[0]


def test_a_doubtful_transfer_target_triggers_a_rethink(context):
    injure(context, FREE_MID_NEW, status="d", chance=50)
    assert "doubtful" in material_changes(transferring(), context)[0]


def test_a_suspended_target_counts_as_unavailable(context):
    injure(context, FREE_MID_NEW, status="s", chance=None)
    assert "will not play" in material_changes(transferring(), context)[0]


def test_the_outgoing_player_going_lame_is_not_a_reason(context):
    """You are already selling them. Their fitness stopped being your problem."""
    injure(context, MID_LIV, status="u", chance=0)
    assert material_changes(transferring(), context) == []


# ----------------------------------------------------------------- the lineup


def test_a_ruled_out_starter_triggers_a_rethink(context):
    proposal = rolling()
    starter = next(p.element for p in context.my_team.picks if p.is_starter)
    proposal.starting_xi = [p.element for p in context.my_team.picks if p.is_starter]
    injure(context, starter, status="u", chance=0)

    assert "will not play" in material_changes(proposal, context)[0]


def test_a_merely_doubtful_starter_is_left_alone(context):
    """FPL auto-subs a blank. Burning a rethink on a 50% squad player is not worth it."""
    proposal = rolling()
    starter = next(
        p.element
        for p in context.my_team.picks
        if p.is_starter and p.element not in (FWD_CAPTAIN, MID_VICE)
    )
    proposal.starting_xi = [p.element for p in context.my_team.picks if p.is_starter]
    injure(context, starter, status="d", chance=50)

    assert material_changes(proposal, context) == []


def test_an_incoming_player_in_the_xi_is_only_reported_once(context):
    proposal = transferring()
    proposal.starting_xi = [FREE_MID_NEW]
    injure(context, FREE_MID_NEW, status="u", chance=0)

    assert len(material_changes(proposal, context)) == 1


# ---------------------------------------------------------------- instruction


def test_the_instruction_carries_the_reasons_and_asks_for_caution(context):
    injure(context, FWD_CAPTAIN)
    text = rethink_instruction(material_changes(rolling(), context))

    assert "Knock picked up in training." in text
    assert "very little time left" in text
    # Framed as new information, not as a telling-off: nothing was wrong before.
    assert "was wrong at the time" in text
