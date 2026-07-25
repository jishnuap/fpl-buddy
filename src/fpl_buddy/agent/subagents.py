"""Specialist subagents.

Two of them, matching the two decisions that actually move points: who wears the
armband, and whether to spend a transfer. Each gets the same read-only toolset as
the main agent -- the split is about attention, not privilege. Neither returns a
proposal; the top-level agent still owns the single structured output.
"""

from __future__ import annotations

from deepagents.middleware.subagents import SubAgent
from langchain_core.tools import BaseTool

from .prompts import CAPTAINCY_SUBAGENT_PROMPT, SCOUT_SUBAGENT_PROMPT


def build_subagents(tools: list[BaseTool]) -> list[SubAgent]:
    return [
        SubAgent(
            name="captaincy-specialist",
            description=(
                "Decides the captain and vice-captain. Delegate to this before "
                "finalising the armband, especially when two candidates are close."
            ),
            system_prompt=CAPTAINCY_SUBAGENT_PROMPT,
            tools=tools,
        ),
        SubAgent(
            name="transfer-scout",
            description=(
                "Finds and price-checks transfer candidates, or argues for rolling "
                "the transfer. Delegate when the squad has a weak link or an "
                "availability problem."
            ),
            system_prompt=SCOUT_SUBAGENT_PROMPT,
            tools=tools,
        ),
    ]
