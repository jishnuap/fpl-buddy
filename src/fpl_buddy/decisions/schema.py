"""The contract between the LLM and the rest of the system.

The agent's *only* output is a ``Proposal``. It never calls a write endpoint
itself -- it fills in this structure, deterministic code validates it, and a
separate executor performs the POSTs. That split is what keeps a hallucinated
player id from costing real points.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


# Gameweeks are a week apart, so a plan written more than one cycle ahead of its
# own deadline was not merely early -- it was decided during a different
# gameweek, against a squad and a set of prices that have had a full transfer
# window to change. Being written a day or two out is early, not stale; the
# renderers show the lead time either way and reserve the warning for this.
STALE_LEAD_TIME = timedelta(days=7)


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
    def lead_time(self) -> timedelta:
        """How far ahead of its own deadline this plan was written."""
        return self.deadline - self.created_at

    @property
    def is_stale(self) -> bool:
        """Was this decided too early to have seen the team news?"""
        return self.lead_time > STALE_LEAD_TIME

    def describe_lead_time(self) -> str:
        """``"26 days"``, ``"3 hours"``, ``"48 minutes"`` -- for the human summary."""
        seconds = max(self.lead_time.total_seconds(), 0)
        for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
            if seconds >= size:
                count = int(seconds // size)
                return f"{count} {unit}{'s' if count != 1 else ''}"
        return "under a minute"

    @property
    def is_executable(self) -> bool:
        """Safe to POST: no fatal validation issue, not already resolved."""
        if self.is_terminal:
            return False
        return not any(issue.fatal for issue in self.validation_issues)

    @property
    def fatal_issues(self) -> list[ValidationIssue]:
        return [i for i in self.validation_issues if i.fatal]

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
