"""Is this plan still worth submitting, or has the world moved under it?

The guardrails in :mod:`fpl_buddy.decisions.validate` answer "is this legal".
That is not the same question. A proposal can be perfectly legal and still be
the wrong thing to submit, because the captain picked up a knock in Friday's
press conference after the proposal was written.

Before this existed the commit job could only do two things with a changed
world, and both were bad:

* commit anyway -- ``captain_flagged`` was non-fatal, so an injured captain
  went in silently; or
* refuse everything -- ``target_unavailable`` is fatal, so one unavailable
  transfer target blocked the transfers, the armband and the lineup together,
  leaving the gameweek untouched.

What the commit job actually wants is a third option: notice, and re-decide.
This module is the "notice" half.

It deliberately does *not* diff against the proposal's stored snapshot. The
snapshot is rendered text, and reconstructing structured state from it to
compute a delta would be fragile in exchange for very little. Instead it asks
the simpler question -- is anything about this plan a problem *now* -- against a
freshly built context. The cost of that choice is one wasted re-run in the rare
case where the agent knowingly picked a flagged player and nothing has since
changed. The benefit is that it cannot miss a change it failed to record.
"""

from __future__ import annotations

from ..data.context import DecisionContext
from .schema import AgentProposal

# Statuses that mean "will not play", as opposed to a doubt.
UNAVAILABLE = ("u", "n", "s")


def material_changes(proposal: AgentProposal, context: DecisionContext) -> list[str]:
    """Reasons this plan should be re-decided rather than submitted as-is.

    Empty list means the plan still stands. Each entry is a human-readable
    sentence, used in logs, in the notification, and in the instruction handed
    back to the agent -- so they are written to be read by all three.
    """
    reasons: list[str] = []
    bootstrap = context.bootstrap

    def describe(element_id: int) -> str:
        player = bootstrap.player(element_id)
        if player is None:
            return f"id {element_id}"
        detail = player.news or f"status '{player.status}'"
        chance = player.chance_of_playing_next_round
        odds = f", {chance}% chance" if chance is not None else ""
        return f"{player.web_name} ({detail}{odds})"

    # The armband is the highest-variance call in the proposal, so it gets the
    # lowest bar: a doubt is enough to reconsider, not just a ruling-out.
    for role, element_id in (
        ("captain", proposal.captaincy.captain_id),
        ("vice-captain", proposal.captaincy.vice_captain_id),
    ):
        player = bootstrap.player(element_id)
        if player is not None and player.is_flagged:
            reasons.append(f"The proposed {role} is now flagged: {describe(element_id)}.")

    # Buying someone who will not play is the most expensive mistake available
    # here, because a transfer cannot be undone before the deadline.
    for move in proposal.transfers:
        player = bootstrap.player(move.element_in)
        if player is None:
            continue
        if player.status in UNAVAILABLE:
            reasons.append(f"Transfer target {describe(move.element_in)} will not play.")
        elif player.is_flagged:
            reasons.append(f"Transfer target {describe(move.element_in)} is now doubtful.")

    # A flagged starter is survivable -- FPL auto-subs a blank. One who is
    # ruled out outright is a wasted slot, and worth spending a rethink on.
    for element_id in proposal.starting_xi:
        if element_id in {m.element_in for m in proposal.transfers}:
            continue  # already reported above, and more precisely
        player = bootstrap.player(element_id)
        if player is not None and player.status in UNAVAILABLE:
            reasons.append(f"Starting XI player {describe(element_id)} will not play.")

    return reasons


def rethink_instruction(reasons: list[str]) -> str:
    """Tell the agent why its previous answer is being thrown out.

    Phrased as fresh facts rather than as a mistake: nothing was wrong when the
    proposal was written, the team news simply arrived afterwards.
    """
    return "\n".join(
        [
            "This is a re-run shortly before the deadline. A proposal already exists "
            "for this gameweek, but the team news has changed since it was written:",
            "",
            *(f"  - {reason}" for reason in reasons),
            "",
            "Nothing about the earlier proposal was wrong at the time; this is newer "
            "information. Produce a complete, fresh proposal for the squad and the "
            "availability as they stand right now. There is very little time left "
            "before the deadline, so prefer a safe, legal plan over an ambitious one, "
            "and do not propose a move you cannot justify on the current facts.",
        ]
    )
