"""Deterministic guardrails.

Nothing the LLM says is trusted. Every proposal is re-derived against the live
squad and ``bootstrap-static`` before a single byte is POSTed: squad legality,
budget, club limits, formation, hit ceiling, deadline. A fatal issue blocks
execution outright -- including the automatic deadline commit.

This module is deliberately boring and fully unit-testable. If you only read one
file in this repo, read this one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ..config import Settings
from ..data.context import DecisionContext
from ..fpl.models import MAX_PER_CLUB, SQUAD_REQUIREMENTS, SQUAD_SIZE
from .schema import AgentProposal, ValidationIssue

logger = logging.getLogger(__name__)

VALID_CHIPS = {"wildcard", "freehit", "bboost", "3xc"}
UNLIMITED_TRANSFER_CHIPS = {"wildcard", "freehit"}

# XI formation bounds, per FPL rules.
FORMATION_BOUNDS = {"GKP": (1, 1), "DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}

# Refuse to execute this close to the deadline -- a POST that lands late is worse
# than no POST, because you cannot tell whether it applied.
EXECUTION_CUTOFF = timedelta(minutes=2)


def validate(
    proposal: AgentProposal,
    context: DecisionContext,
    settings: Settings,
    *,
    for_execution: bool = False,
) -> list[ValidationIssue]:
    """Return every problem found. Empty list == safe to execute."""
    issues: list[ValidationIssue] = []
    add = issues.append

    bootstrap = context.bootstrap
    my_team = context.my_team

    # ------------------------------------------------------------- gameweek
    if proposal.gameweek != context.gameweek.id:
        add(
            ValidationIssue(
                code="wrong_gameweek",
                message=(
                    f"Proposal targets GW{proposal.gameweek} but the next deadline is for "
                    f"GW{context.gameweek.id}."
                ),
            )
        )

    now = datetime.now(UTC)
    if for_execution and context.gameweek.deadline_time - now < EXECUTION_CUTOFF:
        add(
            ValidationIssue(
                code="past_deadline",
                message=(
                    f"Deadline is {context.gameweek.deadline_time.isoformat()}, which is inside "
                    "the execution cutoff. Refusing to submit."
                ),
            )
        )

    # ----------------------------------------------------------------- chip
    chip = proposal.chip
    if chip is not None:
        if chip not in VALID_CHIPS:
            add(ValidationIssue(code="unknown_chip", message=f"'{chip}' is not a valid chip."))
        elif chip not in my_team.chips_available:
            add(
                ValidationIssue(
                    code="chip_unavailable",
                    message=(
                        f"Chip '{chip}' is not available. Available: "
                        f"{', '.join(my_team.chips_available) or 'none'}."
                    ),
                )
            )
    if my_team.active_chip and chip and my_team.active_chip != chip:
        add(
            ValidationIssue(
                code="chip_conflict",
                message=f"Chip '{my_team.active_chip}' is already active for this gameweek.",
            )
        )

    # ------------------------------------------------------------ transfers
    resolved_squad, transfer_issues = _validate_transfers(proposal, context, settings, chip)
    issues.extend(transfer_issues)

    # Only continue structural checks if the squad resolved coherently.
    if resolved_squad is None:
        return issues

    if len(resolved_squad) != SQUAD_SIZE:
        add(
            ValidationIssue(
                code="squad_size",
                message=f"Resulting squad has {len(resolved_squad)} players, expected {SQUAD_SIZE}.",
            )
        )

    # Position counts across the full 15.
    counts: dict[str, int] = dict.fromkeys(SQUAD_REQUIREMENTS, 0)
    club_counts: dict[int, int] = {}
    for element_id in resolved_squad:
        player = bootstrap.player(element_id)
        if player is None:
            add(
                ValidationIssue(
                    code="unknown_player",
                    message=f"Element id {element_id} does not exist in bootstrap-static.",
                )
            )
            continue
        counts[player.position] = counts.get(player.position, 0) + 1
        club_counts[player.team] = club_counts.get(player.team, 0) + 1

    for position, required in SQUAD_REQUIREMENTS.items():
        if counts.get(position, 0) != required:
            add(
                ValidationIssue(
                    code="illegal_squad_shape",
                    message=(
                        f"Resulting squad has {counts.get(position, 0)} {position}, "
                        f"needs exactly {required}."
                    ),
                )
            )

    for team_id, count in club_counts.items():
        if count > MAX_PER_CLUB:
            club = bootstrap.team(team_id)
            add(
                ValidationIssue(
                    code="club_limit",
                    message=(
                        f"{count} players from {club.name if club else team_id}; "
                        f"the limit is {MAX_PER_CLUB}."
                    ),
                )
            )

    # ----------------------------------------------------------- captaincy
    issues.extend(_validate_captaincy(proposal, context, resolved_squad))

    # ------------------------------------------------------- XI / bench
    issues.extend(_validate_lineup(proposal, context, resolved_squad))

    # ------------------------------------------------------------ transfers-in availability
    for move in proposal.transfers:
        player = bootstrap.player(move.element_in)
        if player is None:
            continue
        if player.status in ("u", "n"):
            add(
                ValidationIssue(
                    code="target_unavailable",
                    message=(
                        f"{player.web_name} has status '{player.status}' "
                        f"({player.news or 'unavailable'}) -- refusing to transfer them in."
                    ),
                )
            )
        elif player.is_flagged:
            add(
                ValidationIssue(
                    code="target_flagged",
                    message=(
                        f"{player.web_name} is flagged "
                        f"({player.chance_of_playing_next_round}% chance): "
                        f"{player.news or 'no detail'}."
                    ),
                    fatal=False,
                )
            )

    return issues


# --------------------------------------------------------------------------- #


def _validate_transfers(
    proposal: AgentProposal,
    context: DecisionContext,
    settings: Settings,
    chip: str | None,
) -> tuple[list[int] | None, list[ValidationIssue]]:
    """Resolve prices, check budget/duplication, return the post-transfer squad."""
    issues: list[ValidationIssue] = []
    add = issues.append

    bootstrap = context.bootstrap
    my_team = context.my_team
    squad = [p.element for p in my_team.picks]

    outgoing: set[int] = set()
    incoming: set[int] = set()

    for move in proposal.transfers:
        if move.element_out == move.element_in:
            add(
                ValidationIssue(
                    code="self_transfer",
                    message=f"Transfer has the same player in and out (id {move.element_in}).",
                )
            )
            continue

        pick = my_team.pick_for(move.element_out)
        if pick is None:
            add(
                ValidationIssue(
                    code="not_in_squad",
                    message=(
                        f"Cannot transfer out element {move.element_out} "
                        f"({move.player_out_name or 'unnamed'}) -- not in your squad."
                    ),
                )
            )
            continue
        if move.element_out in outgoing:
            add(
                ValidationIssue(
                    code="duplicate_out",
                    message=f"Element {move.element_out} is transferred out more than once.",
                )
            )
            continue

        target = bootstrap.player(move.element_in)
        if target is None:
            add(
                ValidationIssue(
                    code="unknown_target",
                    message=f"Element {move.element_in} does not exist.",
                )
            )
            continue
        if move.element_in in squad and move.element_in not in outgoing:
            add(
                ValidationIssue(
                    code="already_owned",
                    message=f"{target.web_name} is already in your squad.",
                )
            )
            continue
        if move.element_in in incoming:
            add(
                ValidationIssue(
                    code="duplicate_in",
                    message=f"{target.web_name} is transferred in more than once.",
                )
            )
            continue

        # Authoritative prices -- overwrite whatever the model guessed.
        if pick.selling_price:
            move.selling_price = pick.selling_price
        else:
            # my-team didn't give us a selling price; fall back to the live price.
            # If the player isn't even in bootstrap-static there is nothing to
            # fall back to, and guessing here would send a wrong valuation.
            outgoing_player = bootstrap.player(move.element_out)
            if outgoing_player is None:
                add(
                    ValidationIssue(
                        code="unknown_selling_price",
                        message=(
                            f"Cannot determine a selling price for element "
                            f"{move.element_out}: it has none in your squad and does not "
                            "exist in bootstrap-static."
                        ),
                    )
                )
                continue
            move.selling_price = outgoing_player.now_cost
        move.purchase_price = target.now_cost

        outgoing.add(move.element_out)
        incoming.add(move.element_in)

    if issues:
        # Squad state is untrustworthy once any transfer is malformed.
        return None, issues

    # --------------------------------------------------------------- budget
    proceeds = sum(m.selling_price or 0 for m in proposal.transfers)
    outlay = sum(m.purchase_price or 0 for m in proposal.transfers)
    remaining = my_team.bank + proceeds - outlay
    if remaining < 0:
        add(
            ValidationIssue(
                code="over_budget",
                message=(
                    f"Short by £{abs(remaining) / 10:.1f}m "
                    f"(bank £{my_team.bank / 10:.1f}m + sales £{proceeds / 10:.1f}m "
                    f"- purchases £{outlay / 10:.1f}m)."
                ),
            )
        )

    # ----------------------------------------------------------------- hits
    n = len(proposal.transfers)
    expected_hit = (
        0 if chip in UNLIMITED_TRANSFER_CHIPS else max(0, n - my_team.free_transfers) * 4
    )

    if proposal.points_hit != expected_hit:
        add(
            ValidationIssue(
                code="hit_mismatch",
                message=(
                    f"Proposal claims a -{proposal.points_hit} hit but {n} transfer(s) with "
                    f"{my_team.free_transfers} free transfer(s) costs -{expected_hit}."
                ),
                fatal=False,
            )
        )
        proposal.points_hit = expected_hit

    if expected_hit > settings.max_points_hit:
        add(
            ValidationIssue(
                code="hit_exceeds_limit",
                message=(
                    f"This plan costs -{expected_hit} points but MAX_POINTS_HIT is "
                    f"{settings.max_points_hit}."
                ),
            )
        )

    resolved = [e for e in squad if e not in outgoing] + sorted(incoming)
    return resolved, issues


def _validate_captaincy(
    proposal: AgentProposal, context: DecisionContext, squad: list[int]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    cap = proposal.captaincy
    bootstrap = context.bootstrap

    if cap.captain_id == cap.vice_captain_id:
        issues.append(
            ValidationIssue(
                code="captain_equals_vice",
                message="Captain and vice-captain are the same player.",
            )
        )

    for role, element_id in (("captain", cap.captain_id), ("vice-captain", cap.vice_captain_id)):
        if element_id not in squad:
            issues.append(
                ValidationIssue(
                    code="captain_not_in_squad",
                    message=f"Proposed {role} (id {element_id}) is not in the resulting squad.",
                )
            )
            continue
        player = bootstrap.player(element_id)
        if player and player.is_flagged:
            issues.append(
                ValidationIssue(
                    code="captain_flagged",
                    message=(
                        f"Proposed {role} {player.web_name} is flagged "
                        f"({player.chance_of_playing_next_round}%): {player.news or 'no detail'}."
                    ),
                    fatal=False,
                )
            )
    return issues


def _validate_lineup(
    proposal: AgentProposal, context: DecisionContext, squad: list[int]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    xi, bench = proposal.starting_xi, proposal.bench_order

    if not xi and not bench:
        return issues  # leaving the lineup alone is fine

    if len(xi) != 11:
        issues.append(
            ValidationIssue(
                code="xi_size", message=f"Starting XI has {len(xi)} players, expected 11."
            )
        )
    if len(bench) != 4:
        issues.append(
            ValidationIssue(
                code="bench_size", message=f"Bench has {len(bench)} players, expected 4."
            )
        )

    combined = xi + bench
    if len(set(combined)) != len(combined):
        issues.append(
            ValidationIssue(code="lineup_duplicates", message="A player appears twice in the lineup.")
        )
    if set(combined) != set(squad):
        missing = set(squad) - set(combined)
        extra = set(combined) - set(squad)
        issues.append(
            ValidationIssue(
                code="lineup_mismatch",
                message=(
                    "Lineup does not cover exactly the resulting squad. "
                    f"Missing: {sorted(missing)}. Not owned: {sorted(extra)}."
                ),
            )
        )

    if len(xi) == 11:
        counts: dict[str, int] = {}
        for element_id in xi:
            player = context.bootstrap.player(element_id)
            if player:
                counts[player.position] = counts.get(player.position, 0) + 1
        for position, (low, high) in FORMATION_BOUNDS.items():
            got = counts.get(position, 0)
            if not (low <= got <= high):
                issues.append(
                    ValidationIssue(
                        code="illegal_formation",
                        message=f"XI has {got} {position}; legal range is {low}-{high}.",
                    )
                )

    if bench:
        first = context.bootstrap.player(bench[0])
        if first and first.position != "GKP":
            issues.append(
                ValidationIssue(
                    code="bench_keeper_order",
                    message="Bench slot 12 must be the reserve goalkeeper.",
                    fatal=False,
                )
            )

    if proposal.captaincy.captain_id in bench:
        issues.append(
            ValidationIssue(
                code="captain_benched", message="The proposed captain is on the bench."
            )
        )
    return issues


def build_picks_payload(proposal: AgentProposal, context: DecisionContext, squad: list[int]) -> list[dict]:
    """Turn a validated proposal into the 15-slot ``picks`` array for my-team.

    If the agent left the lineup empty, the current XI/bench order is preserved
    and only the armbands move.
    """
    cap = proposal.captaincy

    if proposal.starting_xi and proposal.bench_order:
        ordered = list(proposal.starting_xi) + list(proposal.bench_order)
    else:
        current = sorted(context.my_team.picks, key=lambda p: p.position)
        ordered = [p.element for p in current]
        # Splice in any incoming players in place of the ones they replaced so
        # positions stay stable.
        replacement = {m.element_out: m.element_in for m in proposal.transfers}
        ordered = [replacement.get(e, e) for e in ordered]

    if sorted(ordered) != sorted(squad):
        raise ValueError("Cannot build picks payload: lineup does not match the resulting squad.")

    return [
        {
            "element": element_id,
            "position": index + 1,
            "is_captain": element_id == cap.captain_id,
            "is_vice_captain": element_id == cap.vice_captain_id,
        }
        for index, element_id in enumerate(ordered)
    ]


def resolved_squad_ids(proposal: AgentProposal, context: DecisionContext) -> list[int]:
    """The 15 element ids the squad will contain after the transfers apply."""
    out = {m.element_out for m in proposal.transfers}
    incoming = sorted({m.element_in for m in proposal.transfers})
    return [p.element for p in context.my_team.picks if p.element not in out] + incoming
