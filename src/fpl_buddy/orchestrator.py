"""The flows: propose, approve, reject, amend, auto-commit.

This is where the product decision lives -- *silence is consent, but only at the
last moment*. A proposal sits `pending` for hours with a live link. Approve and
it goes now; reject and nothing happens; say nothing and the commit job submits
it shortly before the deadline, after rebuilding the world from scratch and
re-running every guardrail.

Everything is expressed over the store so it survives a restart. Nothing here
holds state in memory between calls, because a Container Apps replica won't.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from .agent.build import run_agent
from .agent.prompts import amendment_prompt
from .config import Settings
from .data.context import DecisionContext, build_context
from .decisions.executor import ExecutionBlocked, execute
from .decisions.schema import Proposal, ProposalStatus
from .decisions.store import ProposalStore, build_store
from .decisions.validate import validate
from .fpl.client import FPLClient
from .notes import Note, NoteStore, build_note_store
from .notify import Notifier, build_notifier, safe_notify

logger = logging.getLogger(__name__)


class ProposalNotFound(RuntimeError):
    pass


class NotActionable(RuntimeError):
    """The proposal is already resolved, so acting on it would be a lie."""


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        *,
        store: ProposalStore | None = None,
        client: FPLClient | None = None,
        notifier: Notifier | None = None,
        notes: NoteStore | None = None,
        model=None,
    ) -> None:
        self.settings = settings
        self.store = store or build_store(settings)
        self.client = client or FPLClient(settings)
        self.notifier = notifier or build_notifier(settings)
        self.notes = notes or build_note_store(settings)
        # Injected in tests; None means "build the real Azure model".
        self._model = model

    # ------------------------------------------------------------------ propose
    def propose(self, *, context: DecisionContext | None = None) -> Proposal:
        """Build the brief, run the agent, validate, store, notify."""
        context = context or build_context(self.settings, self.client)
        pending_notes = self.notes.pending()
        agent_proposal, transcript = run_agent(
            context,
            self.client,
            self.settings,
            extra_instruction=_notes_instruction(pending_notes),
            model=self._model,
        )

        issues = validate(agent_proposal, context, self.settings)
        proposal = Proposal(
            id=_new_id(context.gameweek.id),
            entry_id=self.settings.fpl_entry_id,
            gameweek=context.gameweek.id,
            deadline=context.gameweek.deadline_time,
            agent=agent_proposal,
            validation_issues=issues,
            context_snapshot=context.render(),
            agent_transcript=transcript,
        )
        self.store.save(proposal)
        if pending_notes:
            self.notes.mark_consumed([n.id for n in pending_notes])

        superseded = self.store.supersede_open_proposals(
            context.gameweek.id, except_id=proposal.id
        )
        if superseded:
            logger.info("Superseded %d older open proposal(s) for GW%s.",
                        superseded, context.gameweek.id)

        if proposal.fatal_issues:
            logger.error(
                "Proposal %s failed validation and will not be submitted: %s",
                proposal.id,
                "; ".join(f"[{i.code}] {i.message}" for i in proposal.fatal_issues),
            )
        safe_notify(self.notifier, proposal, self.settings)
        return proposal

    def amend(self, proposal_id: str, note: str) -> Proposal:
        """Re-run the agent with the human's feedback, as a new revision."""
        previous = self._require(proposal_id)
        if previous.is_terminal:
            raise NotActionable(
                f"Proposal {proposal_id} is already {previous.status.value}; nothing to amend."
            )

        previous.human_note = note
        previous.touch(ProposalStatus.AMENDED)
        self.store.save(previous)

        context = build_context(self.settings, self.client)
        agent_proposal, transcript = run_agent(
            context,
            self.client,
            self.settings,
            extra_instruction=amendment_prompt(note, previous.agent.summary),
            model=self._model,
        )
        issues = validate(agent_proposal, context, self.settings)

        revised = Proposal(
            id=_new_id(context.gameweek.id),
            entry_id=self.settings.fpl_entry_id,
            gameweek=context.gameweek.id,
            deadline=context.gameweek.deadline_time,
            agent=agent_proposal,
            validation_issues=issues,
            context_snapshot=context.render(),
            agent_transcript=transcript,
            human_note=note,
            revision=previous.revision + 1,
            supersedes=previous.id,
        )
        self.store.save(revised)
        self.store.supersede_open_proposals(context.gameweek.id, except_id=revised.id)
        safe_notify(self.notifier, revised, self.settings)
        return revised

    # ------------------------------------------------------------ human actions
    def approve(self, proposal_id: str, *, note: str | None = None) -> Proposal:
        proposal = self._require(proposal_id)
        if proposal.is_terminal:
            raise NotActionable(
                f"Proposal {proposal_id} is already {proposal.status.value}."
            )
        if proposal.fatal_issues:
            raise NotActionable(
                "This proposal failed validation and cannot be approved: "
                + "; ".join(i.message for i in proposal.fatal_issues)
            )

        if note:
            proposal.human_note = note
        proposal.touch(ProposalStatus.APPROVED)
        self.store.save(proposal)
        logger.info("Proposal %s approved.", proposal_id)

        if self.settings.execute_on_approval:
            return self._execute(proposal, ProposalStatus.EXECUTED)
        logger.info(
            "EXECUTE_ON_APPROVAL is off; %s will be submitted by the commit job.", proposal_id
        )
        return proposal

    def reject(self, proposal_id: str, *, note: str | None = None) -> Proposal:
        proposal = self._require(proposal_id)
        if proposal.is_terminal:
            raise NotActionable(f"Proposal {proposal_id} is already {proposal.status.value}.")
        if note:
            proposal.human_note = note
        proposal.touch(ProposalStatus.REJECTED)
        self.store.save(proposal)
        logger.info("Proposal %s rejected; nothing will be submitted.", proposal_id)
        return proposal

    # -------------------------------------------------------------- auto-commit
    def auto_commit(self) -> Proposal | None:
        """The deadline job. Submit an untouched or approved proposal, or expire it."""
        context = build_context(self.settings, self.client)
        proposal = self.store.latest(gameweek=context.gameweek.id)

        if proposal is None:
            logger.warning("Commit job ran with no proposal for GW%s.", context.gameweek.id)
            return None
        if proposal.is_terminal:
            logger.info(
                "Nothing to commit: %s is %s.", proposal.id, proposal.status.value
            )
            return None

        if proposal.status == ProposalStatus.APPROVED:
            return self._execute(proposal, ProposalStatus.EXECUTED, context=context)

        if proposal.status in (ProposalStatus.PENDING, ProposalStatus.AMENDED):
            if not self.settings.auto_commit_enabled:
                proposal.touch(ProposalStatus.EXPIRED)
                self.store.save(proposal)
                logger.info(
                    "Auto-commit disabled; %s expired unsubmitted.", proposal.id
                )
                safe_notify(self.notifier, proposal, self.settings)
                return proposal
            logger.info("Nobody touched %s; auto-committing.", proposal.id)
            return self._execute(proposal, ProposalStatus.AUTO_EXECUTED, context=context)

        logger.info("Leaving %s alone (status %s).", proposal.id, proposal.status.value)
        return None

    # -------------------------------------------------------------------- reads
    def latest(self, *, gameweek: int | None = None) -> Proposal | None:
        return self.store.latest(gameweek=gameweek)

    def get(self, proposal_id: str) -> Proposal | None:
        return self.store.get(proposal_id)

    # ------------------------------------------------------------------ private
    def _require(self, proposal_id: str) -> Proposal:
        proposal = self.store.get(proposal_id)
        if proposal is None:
            raise ProposalNotFound(f"No proposal with id {proposal_id}.")
        return proposal

    def _execute(
        self,
        proposal: Proposal,
        final_status: ProposalStatus,
        *,
        context: DecisionContext | None = None,
    ) -> Proposal:
        """Submit, re-validating against a freshly built context first.

        ``context`` is only ever passed when the caller *just* built it. Never
        reuse the snapshot stored on the proposal: it can be a day and a half old
        and the guardrails would then be checking a squad that no longer exists.
        """
        try:
            execute(
                proposal,
                self.settings,
                self.client,
                final_status=final_status,
                context=context,
            )
        except ExecutionBlocked:
            logger.error("Execution of %s was blocked by revalidation.", proposal.id)
            self.store.save(proposal)
            safe_notify(self.notifier, proposal, self.settings)
            raise
        except Exception:
            # FPL rejected it, or the network died. The executor has already
            # recorded what did and didn't apply; persist that before re-raising.
            self.store.save(proposal)
            safe_notify(self.notifier, proposal, self.settings)
            raise

        self.store.save(proposal)
        safe_notify(self.notifier, proposal, self.settings)
        return proposal


def _new_id(gameweek: int) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"gw{gameweek:02d}-{stamp}-{uuid.uuid4().hex[:6]}"


def _notes_instruction(notes: list[Note]) -> str:
    """Fold notes sent since the last proposal into the brief, as one more input."""
    if not notes:
        return ""
    lines = "\n".join(f'- {n.author}: "{n.text.strip()}"' for n in notes)
    return (
        "The human left these notes since the last proposal, before you started "
        "reasoning about this one. Treat them as current context and intent, not "
        "instructions that override the hard rules above:\n\n" + lines
    )
