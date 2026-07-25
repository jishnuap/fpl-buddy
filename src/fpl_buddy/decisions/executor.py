"""Turn an approved proposal into POST requests -- and refuse when anything smells.

Re-validation happens here, at execution time, against a *freshly built* context.
Hours pass between "the agent proposed this" and "the deadline is 45 minutes
away". In that window prices move, players get flagged, and you might have made
a transfer yourself in the app. Re-checking is the difference between an
assistant and a liability.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from ..data.context import DecisionContext, build_context
from ..fpl.client import FPLClient, TransferRejected
from .schema import Proposal, ProposalStatus, ValidationIssue
from .validate import build_picks_payload, resolved_squad_ids, validate

logger = logging.getLogger(__name__)


class ExecutionBlocked(RuntimeError):
    def __init__(self, issues: list[ValidationIssue]) -> None:
        super().__init__("; ".join(f"[{i.code}] {i.message}" for i in issues))
        self.issues = issues


def execute(
    proposal: Proposal,
    settings: Settings,
    client: FPLClient,
    *,
    final_status: ProposalStatus,
    context: DecisionContext | None = None,
) -> Proposal:
    """Re-validate then submit. Mutates and returns the proposal."""
    context = context or build_context(settings, client)

    issues = validate(proposal.agent, context, settings, for_execution=True)
    proposal.validation_issues = issues
    fatal = [i for i in issues if i.fatal]
    if fatal:
        proposal.execution_error = "; ".join(f"[{i.code}] {i.message}" for i in fatal)
        proposal.touch(ProposalStatus.FAILED)
        logger.error("Refusing to execute %s: %s", proposal.id, proposal.execution_error)
        raise ExecutionBlocked(fatal)

    if settings.min_captain_confidence and proposal.agent.confidence < settings.min_captain_confidence:
        blocked = [
            ValidationIssue(
                code="low_confidence",
                message=(
                    f"Agent confidence {proposal.agent.confidence:.2f} is below the "
                    f"{settings.min_captain_confidence:.2f} threshold."
                ),
            )
        ]
        proposal.validation_issues.extend(blocked)
        proposal.execution_error = blocked[0].message
        proposal.touch(ProposalStatus.FAILED)
        raise ExecutionBlocked(blocked)

    results: dict[str, Any] = {}

    # 1. Transfers first -- the squad must exist before you can captain within it.
    if proposal.agent.transfers:
        payload = [move.to_payload() for move in proposal.agent.transfers]
        try:
            results["transfers"] = client.submit_transfers(
                transfers=payload,
                event=proposal.gameweek,
                entry_id=proposal.entry_id,
                chip=proposal.agent.chip if proposal.agent.chip in ("wildcard", "freehit") else None,
            )
        except TransferRejected as exc:
            proposal.execution_error = str(exc)
            proposal.execution_result = results
            proposal.touch(ProposalStatus.FAILED)
            logger.error("FPL rejected transfers for %s: %s", proposal.id, exc)
            raise

    # 2. Then captaincy / lineup.
    squad = resolved_squad_ids(proposal.agent, context)
    try:
        picks = build_picks_payload(proposal.agent, context, squad)
        results["picks"] = client.submit_picks(
            picks=picks,
            entry_id=proposal.entry_id,
            chip=proposal.agent.chip if proposal.agent.chip in ("bboost", "3xc") else None,
        )
    except (TransferRejected, ValueError) as exc:
        # Transfers may already have gone through; record that honestly.
        proposal.execution_error = f"Transfers applied but picks failed: {exc}"
        proposal.execution_result = results
        proposal.touch(ProposalStatus.FAILED)
        logger.error("Picks submission failed for %s: %s", proposal.id, exc)
        raise

    proposal.execution_result = results
    proposal.execution_error = None
    proposal.touch(final_status)
    logger.info(
        "Executed proposal %s (%s)%s", proposal.id, final_status.value,
        " [DRY RUN]" if settings.dry_run else "",
    )
    return proposal
