"""The contract between the LLM and the rest of the system.

The agent's *only* output is a ``Proposal``. It never calls a write endpoint
itself -- it fills in this structure, deterministic code validates it, and a
separate executor performs the POSTs. That split is what keeps a hallucinated
player id from costing real points.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProposalStatus(StrEnum):
    PENDING = "pending"  # waiting on you
    APPROVED = "approved"  # you said yes; executor will run
    REJECTED = "rejected"  # you said no; nothing happens
    AMENDED = "amended"  # you sent feedback; agent is re-running
    EXECUTED = "executed"  # POSTed successfully
    AUTO_EXECUTED = "auto_executed"  # deadline arrived untouched, POSTed
    FAILED = "failed"  # execution attempted and rejected by FPL
    EXPIRED = "expired"  # deadline passed with auto-commit disabled
    SUPERSEDED = "superseded"  # a newer proposal replaced this one


TERMINAL_STATUSES = {
    ProposalStatus.REJECTED,
    ProposalStatus.EXECUTED,
    ProposalStatus.AUTO_EXECUTED,
    ProposalStatus.EXPIRED,
    ProposalStatus.SUPERSEDED,
}


class TransferMove(BaseModel):
    """One swap. Prices are in FPL tenths (55 == £5.5m)."""

    model_config = ConfigDict(extra="forbid")

    element_out: int = Field(description="FPL element id of the player leaving your squad.")
    element_in: int = Field(description="FPL element id of the player joining your squad.")
    player_out_name: str = Field(default="", description="Display name, for the human summary.")
    player_in_name: str = Field(default="", description="Display name, for the human summary.")
    reason: str = Field(default="", description="Why this swap, in one or two sentences.")

    # Filled in by the validator from authoritative data, never by the LLM.
    selling_price: int | None = Field(default=None, description="Set by the validator.")
    purchase_price: int | None = Field(default=None, description="Set by the validator.")

    def to_payload(self) -> dict[str, int]:
        if self.selling_price is None or self.purchase_price is None:
            raise ValueError("Transfer prices were never resolved; run validation first.")
        return {
            "element_in": self.element_in,
            "element_out": self.element_out,
            "purchase_price": self.purchase_price,
            "selling_price": self.selling_price,
        }


class CaptaincyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captain_id: int = Field(description="FPL element id of the captain.")
    vice_captain_id: int = Field(description="FPL element id of the vice-captain.")
    captain_name: str = ""
    vice_captain_name: str = ""
    reason: str = Field(default="", description="Why this armband, in two or three sentences.")
    alternatives_considered: list[str] = Field(default_factory=list)


class AgentProposal(BaseModel):
    """Structured output the LLM must produce."""

    model_config = ConfigDict(extra="forbid")

    gameweek: int = Field(description="The gameweek this proposal is for.")
    captaincy: CaptaincyDecision
    transfers: list[TransferMove] = Field(
        default_factory=list,
        description="Empty list means 'roll the transfer' -- that is a valid, often correct answer.",
    )
    starting_xi: list[int] = Field(
        default_factory=list,
        description=(
            "Exactly 11 element ids for the starting XI, or empty to leave the current XI "
            "untouched. Must be a legal formation."
        ),
    )
    bench_order: list[int] = Field(
        default_factory=list,
        description=(
            "Exactly 4 element ids, reserve keeper first, then outfield in auto-sub priority. "
            "Empty to leave the bench untouched."
        ),
    )
    # The lineup is a decision, not just a constraint to satisfy, so it carries
    # its reasoning the way captaincy and transfers do. Kept flat rather than
    # wrapped in a LineupDecision beside CaptaincyDecision: the two lists are
    # read by the validator, the executor and the staleness check, and nesting
    # them would churn every one of those call sites to no behavioural end.
    lineup_reason: str = Field(
        default="",
        description=(
            "Why this XI and this bench order. Name anyone you benched or promoted "
            "and say what decided it. If you left the lineup as it was, say that and "
            "why -- 'no change' is a real answer and a useful one."
        ),
    )
    lineup_alternatives: list[str] = Field(
        default_factory=list,
        description="Line-up changes you considered and rejected, one per entry.",
    )
    chip: str | None = Field(
        default=None,
        description="One of wildcard, freehit, bboost, 3xc -- or null. Default to null.",
    )
    points_hit: int = Field(
        default=0, description="Points cost of the extra transfers (0, 4, 8, ...)."
    )
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0, description="How confident you are in this plan overall."
    )
    summary: str = Field(description="Three to six sentences a human can read in 20 seconds.")
    risks: list[str] = Field(
        default_factory=list,
        description="What could make this wrong: late fitness tests, rotation, price traps.",
    )


class ValidationIssue(BaseModel):
    code: str
    message: str
    fatal: bool = True


class Proposal(BaseModel):
    """The stored, validated record. This is what the approval flow acts on."""

    model_config = ConfigDict(extra="ignore")

    id: str
    entry_id: int
    gameweek: int
    deadline: datetime
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    agent: AgentProposal
    validation_issues: list[ValidationIssue] = Field(default_factory=list)

    # The lineup as it stood when the agent was asked. Recorded here rather than
    # re-derived at render time because a proposal is reviewed long after it was
    # written, and "what would change if I approve this" has to be answerable
    # from the stored record alone -- not from whatever the squad looks like now.
    previous_starting_xi: list[int] = Field(default_factory=list)
    previous_bench_order: list[int] = Field(default_factory=list)
    squad_names: dict[int, str] = Field(
        default_factory=dict,
        description="Element id -> display name, so a stored proposal renders without a fetch.",
    )

    # Audit trail
    context_snapshot: str = Field(default="", description="The brief the agent reasoned over.")
    agent_transcript: str = Field(default="", description="Trimmed agent reasoning, for review.")
    execution_result: dict[str, Any] | None = None
    execution_error: str | None = None
    human_note: str | None = None
    revision: int = 0
    supersedes: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_executable(self) -> bool:
        """Safe to POST: no fatal validation issue, not already resolved."""
        if self.is_terminal:
            return False
        return not any(issue.fatal for issue in self.validation_issues)

    @property
    def fatal_issues(self) -> list[ValidationIssue]:
        return [i for i in self.validation_issues if i.fatal]

    def name_for(self, element_id: int) -> str:
        return self.squad_names.get(element_id) or f"id={element_id}"

    def lineup_changes(self) -> tuple[list[str], list[str]]:
        """``(promoted, benched)`` names, comparing the proposed XI to the old one.

        Players arriving or leaving by transfer are deliberately excluded. A
        bought player appearing in the XI is not a substitution decision, it is
        the transfer you already read about two lines up, and listing it as one
        buries the real change -- a fit player being dropped -- in noise.
        """
        if not self.agent.starting_xi or not self.previous_starting_xi:
            return [], []
        before, after = set(self.previous_starting_xi), set(self.agent.starting_xi)
        moved_in = {move.element_in for move in self.agent.transfers}
        moved_out = {move.element_out for move in self.agent.transfers}
        promoted = sorted(after - before - moved_in)
        benched = sorted(before - after - moved_out)
        return (
            [self.name_for(element_id) for element_id in promoted],
            [self.name_for(element_id) for element_id in benched],
        )

    def bench_order_changed(self) -> bool:
        """Order is the whole point of the bench, so compare it as a sequence."""
        if not self.agent.bench_order or not self.previous_bench_order:
            return False
        moved = {m.element_in for m in self.agent.transfers} | {
            m.element_out for m in self.agent.transfers
        }
        before = [e for e in self.previous_bench_order if e not in moved]
        after = [e for e in self.agent.bench_order if e not in moved]
        return before != after

    def touch(self, status: ProposalStatus | None = None) -> None:
        if status is not None:
            self.status = status
        self.updated_at = datetime.now(UTC)

    def headline(self) -> str:
        cap = self.agent.captaincy
        bits = [f"(C) {cap.captain_name or cap.captain_id}"]
        if self.agent.transfers:
            bits += [
                f"{t.player_out_name or t.element_out} -> {t.player_in_name or t.element_in}"
                for t in self.agent.transfers
            ]
        else:
            bits.append("no transfers (rolling)")
        if self.agent.points_hit:
            bits.append(f"-{self.agent.points_hit} hit")
        if self.agent.chip:
            bits.append(f"chip: {self.agent.chip}")
        return " | ".join(bits)
