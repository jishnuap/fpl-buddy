"""A chat model that returns a fixed structured proposal.

Used wherever a test needs the agent to "decide" something without an Azure
round trip. It finds whichever bound tool carries the response schema and calls
it with the payload it was given, which is exactly what a real model does when
``response_format`` is set.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .conftest import (
    FREE_FWD_EXPENSIVE,
    FREE_MID_NEW,
    FWD_CAPTAIN,
    FWD_LIV,
    MID_LIV,
    MID_VICE,
    NEXT_GAMEWEEK,
)

ROLLING_PROPOSAL: dict = {
    "gameweek": NEXT_GAMEWEEK,
    "captaincy": {
        "captain_id": FWD_CAPTAIN,
        "vice_captain_id": MID_VICE,
        "captain_name": "Vasquez",
        "vice_captain_name": "Hollis",
        "reason": "Home fixture, highest projection, on penalties.",
        "alternatives_considered": ["Oakley: away to a good defence"],
    },
    "transfers": [],
    "starting_xi": [],
    "bench_order": [],
    "chip": None,
    "points_hit": 0,
    "confidence": 0.72,
    "summary": "Roll the transfer; the armband stays on Vasquez.",
    "risks": ["Vasquez was rested midweek."],
}

ONE_TRANSFER_PROPOSAL: dict = {
    **ROLLING_PROPOSAL,
    "transfers": [
        {
            "element_out": MID_LIV,
            "element_in": FREE_MID_NEW,
            "player_out_name": "Hollis",
            "player_in_name": "Abbott",
            "reason": "Better fixtures and cheaper.",
        }
    ],
    "summary": "One transfer: Hollis out, Abbott in.",
}


# The failure seen in production: the armband goes to the top row of a
# league-wide projection board, which is not a player the squad contains.
CAPTAIN_NOT_OWNED_PROPOSAL: dict = {
    **ROLLING_PROPOSAL,
    "captaincy": {
        **ROLLING_PROPOSAL["captaincy"],
        "captain_id": FREE_MID_NEW,
        "captain_name": "Abbott",
    },
    "summary": "Captains a player from the league-wide leaderboard.",
}


# A second armband, for tests that need the re-run to reach a *different*
# answer than the one the team news just invalidated.
OTHER_CAPTAIN_PROPOSAL: dict = {
    **ROLLING_PROPOSAL,
    "captaincy": {
        **ROLLING_PROPOSAL["captaincy"],
        "captain_id": FWD_LIV,
        "captain_name": "Okonkwo",
    },
    "summary": "Armband moves off the injured player.",
}


# Also seen live: two swaps that are each affordable alone, but not together.
OVER_BUDGET_PROPOSAL: dict = {
    **ROLLING_PROPOSAL,
    "transfers": [
        {
            "element_out": FWD_LIV,
            "element_in": FREE_FWD_EXPENSIVE,
            "player_out_name": "Okonkwo",
            "player_in_name": "Too dear",
            "reason": "Ignores the price tag.",
        }
    ],
    "summary": "Buys a player the bank cannot cover.",
}


def tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name") or tool.get("function", {}).get("name", ""))
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", tool))


class FakeStructuredModel(BaseChatModel):
    payload: dict = ROLLING_PROPOSAL
    bound_tool_names: list[str] = []
    calls: int = 0
    seen_prompts: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "fake-structured"

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeStructuredModel:
        self.bound_tool_names = [tool_name(t) for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls += 1
        self.seen_prompts.append(str(messages[-1].content))
        schema_tool = next(
            (n for n in self.bound_tool_names if "agentproposal" in n.lower()), None
        )
        if schema_tool is None:  # pragma: no cover - would mean the wiring changed
            raise AssertionError(f"No structured-output tool bound; got {self.bound_tool_names}")
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {"name": schema_tool, "args": self._payload(), "id": "call_1"}
                        ],
                    )
                )
            ]
        )

    def _payload(self) -> dict:
        return self.payload


class FakeChangingModel(FakeStructuredModel):
    """Answers differently each agent run, so a repair loop can be observed.

    ``payloads`` is consumed one entry per ``run_agent`` attempt; the last entry
    repeats once exhausted. A single-payload fake can only ever show that the
    loop terminates, not that a correction is picked up.
    """

    payloads: list[dict] = []
    attempt: int = 0

    def _payload(self) -> dict:
        return self.payloads[min(self.attempt - 1, len(self.payloads) - 1)]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        # No counter to keep in sync: the repair loop appends one human turn per
        # retry, so the number of human messages *is* the attempt number.
        self.attempt = sum(1 for m in messages if getattr(m, "type", "") == "human")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
