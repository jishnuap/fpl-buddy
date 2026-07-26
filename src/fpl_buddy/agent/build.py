"""Wire up the Azure model and the deep agent.

Two things here are easy to get wrong and worth stating plainly:

* ``model`` gets an ``AzureChatOpenAI`` **instance**, not a ``"provider:model"``
  string. The string form has nowhere to put the Azure endpoint, deployment name
  and api-version, so it quietly talks to the wrong place (or nowhere).
* ``response_format`` is set to ``AgentProposal``, which is what makes the run
  end in a typed object instead of prose we would have to parse. ``ToolStrategy``
  is used explicitly because it works across deployments that do not support
  provider-side structured output.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage
from langchain_openai import AzureChatOpenAI

from ..config import Settings
from ..data.context import DecisionContext
from ..decisions.schema import AgentProposal, ValidationIssue
from ..fpl.client import FPLClient
from .prompts import SYSTEM_PROMPT
from .subagents import build_subagents
from .tools import build_tools

logger = logging.getLogger(__name__)

# Scope for Entra ID tokens against Azure OpenAI / AI Foundry.
COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"

# The agent can loop over tools and subagents; cap it so a confused run ends.
RECURSION_LIMIT = 60


class AgentConfigError(RuntimeError):
    """The model could not be configured -- missing endpoint, key, or identity."""


def build_model(settings: Settings) -> AzureChatOpenAI:
    if not settings.azure_openai_endpoint:
        raise AgentConfigError("AZURE_OPENAI_ENDPOINT is not set.")

    kwargs: dict[str, Any] = {
        "azure_endpoint": settings.azure_openai_endpoint,
        "azure_deployment": settings.azure_openai_deployment,
        "api_version": settings.azure_openai_api_version,
        "timeout": settings.http_timeout_seconds * 4,  # agent turns are slower than an API read
        "max_retries": 3,
    }

    if settings.azure_openai_auth == "managed_identity":
        # On Container Apps this picks up the assigned identity with no secret in
        # the environment at all. Locally it falls through to az login.
        try:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        except ImportError as exc:  # pragma: no cover - depends on the azure extra
            raise AgentConfigError(
                "AZURE_OPENAI_AUTH=managed_identity requires the azure extra: "
                "pip install -e '.[azure]'"
            ) from exc
        kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
            DefaultAzureCredential(), COGNITIVE_SERVICES_SCOPE
        )
        logger.info("Azure OpenAI: using managed identity.")
    else:
        key = settings.azure_openai_api_key.get_secret_value()
        if not key:
            raise AgentConfigError(
                "AZURE_OPENAI_API_KEY is empty. Set it, or set "
                "AZURE_OPENAI_AUTH=managed_identity."
            )
        kwargs["api_key"] = key
        logger.info("Azure OpenAI: using API key auth.")

    return AzureChatOpenAI(**kwargs)


def _knowledge_store(settings: Settings):
    """The article archive the tools can search, or None if harvesting is off.

    Deliberately not read from ``context``: the brief holds a recent window so
    its token cost stays fixed, and the tools reach past that window. A failure
    here costs the agent its archive lookups, not the gameweek.
    """
    try:
        from ..knowledge.store import open_archive

        return open_archive(settings)
    except Exception as exc:  # noqa: BLE001 - enrichment, never required
        logger.warning("Could not open the article archive (%s); tools will use the brief.", exc)
        return None


def build_agent(
    context: DecisionContext,
    client: FPLClient,
    settings: Settings,
    *,
    model: Any | None = None,
):
    """Create the deep agent for one gameweek's context.

    ``model`` is injectable so tests can drive the graph with a fake chat model
    and never reach Azure.
    """
    from deepagents import create_deep_agent

    tools = build_tools(context, client, knowledge=_knowledge_store(settings))
    return create_deep_agent(
        model=model if model is not None else build_model(settings),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        subagents=build_subagents(tools),
        response_format=ToolStrategy(AgentProposal),
        name="fpl-buddy",
    )


def run_agent(
    context: DecisionContext,
    client: FPLClient,
    settings: Settings,
    *,
    extra_instruction: str = "",
    model: Any | None = None,
) -> tuple[AgentProposal, str]:
    """Run the agent over the brief and return ``(proposal, transcript)``.

    The transcript is kept for the audit trail: when a proposal looks strange
    six weeks later, the reasoning is the only way to tell a bad model call from
    a bad brief.
    """
    from ..decisions.validate import validate

    agent = build_agent(context, client, settings, model=model)

    brief = context.render()
    prompt = brief if not extra_instruction else f"{brief}\n\n---\n\n{extra_instruction}"

    logger.info("Running agent for GW%s (brief is %d chars).", context.gameweek.id, len(brief))
    messages: list[Any] = [HumanMessage(content=prompt)]
    result: dict = {}
    proposal: AgentProposal | None = None

    for attempt in range(settings.agent_repair_attempts + 1):
        result = agent.invoke(
            {"messages": messages},
            config={"recursion_limit": RECURSION_LIMIT},
        )
        proposal = _structured(result)

        fatal = [issue for issue in validate(proposal, context, settings) if issue.fatal]
        if not fatal:
            if attempt:
                logger.info("Agent repaired its proposal on attempt %d.", attempt + 1)
            break
        if attempt == settings.agent_repair_attempts:
            # Return it anyway. The orchestrator re-validates and stores the
            # issues, so a proposal that cannot be fixed still reaches the human
            # with its problems attached -- better than raising and showing them
            # nothing at all.
            logger.warning(
                "Agent still failed %d guardrail(s) after %d attempt(s); "
                "handing the invalid proposal on for review: %s",
                len(fatal),
                attempt + 1,
                "; ".join(issue.message for issue in fatal),
            )
            break

        logger.warning(
            "Agent proposal failed %d guardrail(s) on attempt %d, asking it to fix: %s",
            len(fatal),
            attempt + 1,
            "; ".join(issue.message for issue in fatal),
        )
        # Continue the same conversation rather than starting over: the tool
        # results and the reasoning that produced the good parts are still in
        # there, so this is a correction rather than a fresh guess.
        messages = list(result.get("messages") or []) + [
            HumanMessage(content=_repair_message(proposal, context, fatal))
        ]

    assert proposal is not None  # the loop runs at least once
    return proposal, _transcript(result)


# --------------------------------------------------------------------------- #


def _structured(result: dict) -> AgentProposal:
    proposal = result.get("structured_response")
    if proposal is None:
        raise AgentConfigError(
            "The agent finished without producing a structured proposal. Last message: "
            f"{_last_text(result)[:500]}"
        )
    if not isinstance(proposal, AgentProposal):
        # A dict comes back when the model's tool call is coerced loosely.
        return AgentProposal.model_validate(proposal)
    return proposal


def _repair_message(
    proposal: AgentProposal, context: DecisionContext, fatal: list[ValidationIssue]
) -> str:
    """Tell the agent what broke, and hand it the arithmetic it got wrong.

    The point is not to repeat the rules -- the system prompt states them
    already, and restating them is what has failed. The point is to compute the
    resulting squad *from the agent's own transfers* and show it, so the correct
    answer is a lookup rather than a derivation.
    """
    from ..decisions.validate import resolved_squad_ids

    squad = resolved_squad_ids(proposal, context)
    lines = []
    for element_id in squad:
        player = context.bootstrap.player(element_id)
        if player is None:
            lines.append(f"  id={element_id} <unknown>")
            continue
        club = context.bootstrap.team(player.team)
        lines.append(
            f"  id={element_id:<4} {player.web_name:<22} "
            f"({club.short_name if club else '?'}, {player.position})"
        )

    return "\n".join(
        [
            "STOP. Your proposal failed the deterministic guardrails and will be thrown "
            "away as it stands.",
            "",
            "What is wrong:",
            *(f"  - {issue.message}" for issue in fatal),
            "",
            "Applying the transfers you yourself proposed, your squad will be exactly "
            f"these {len(squad)} players and nobody else:",
            *lines,
            "",
            "Against that list:",
            "  - captain_id and vice_captain_id must both appear in it, and must differ.",
            "  - starting_xi (11) + bench_order (4) must together be exactly that list -- "
            "every id present once, no id from outside it.",
            "  - a player you transferred out is NOT in it; a player you transferred in IS.",
            "",
            *_budget_lines(proposal, context),
            "Either fix the captaincy and lineup to match those 15, or change the transfers "
            "and make everything consistent with the new squad. Return the corrected, "
            "complete proposal.",
        ]
    )


def _budget_lines(proposal: AgentProposal, context: DecisionContext) -> list[str]:
    """The running total for the whole transfer batch, priced by the API.

    ``transfer_options`` answers "what can I afford if I sell this one player",
    so two swaps that are each affordable alone can be unaffordable together --
    and an agent that overspent will keep overspending unless it is shown the
    combined sum. Prices here are the corrected ones ``validate`` writes back,
    not the ones the agent assumed.
    """
    if not proposal.transfers:
        return []

    def name(element_id: int) -> str:
        player = context.bootstrap.player(element_id)
        return player.web_name if player else f"id {element_id}"

    bank = context.my_team.bank
    rows = [f"  bank                          £{bank / 10:>6.1f}m"]
    for move in proposal.transfers:
        rows.append(f"  + sell {name(move.element_out):<22} £{(move.selling_price or 0) / 10:>6.1f}m")
    for move in proposal.transfers:
        rows.append(f"  - buy  {name(move.element_in):<22} £{(move.purchase_price or 0) / 10:>6.1f}m")

    proceeds = sum(m.selling_price or 0 for m in proposal.transfers)
    outlay = sum(m.purchase_price or 0 for m in proposal.transfers)
    remaining = bank + proceeds - outlay
    verdict = "OVERSPENT" if remaining < 0 else "ok"
    rows.append(f"  = left in the bank            £{remaining / 10:>6.1f}m  <-- {verdict}")

    return [
        "Your budget across ALL the transfers together, at the real prices:",
        *rows,
        "",
        "That total must not go negative. Selling one player only funds the player you "
        "buy to replace him if the sums add up across the whole batch -- check the batch, "
        "not each swap on its own. To fix an overspend, pick a cheaper target or drop a "
        "transfer.",
        "",
    ]


def _last_text(result: dict) -> str:
    messages = result.get("messages") or []
    return _text_of(messages[-1]) if messages else ""


def _text_of(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    # Content blocks: keep the text parts, drop image/tool payloads.
    return " ".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _transcript(result: dict, *, limit: int = 12_000) -> str:
    """Flatten the message history into something a human can skim."""
    lines: list[str] = []
    for message in result.get("messages") or []:
        role = getattr(message, "type", "?")
        text = _text_of(message).strip()
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            lines.append(f"[{role}] -> " + ", ".join(c.get("name", "?") for c in calls))
        if text:
            lines.append(f"[{role}] {text}")
    joined = "\n".join(lines)
    if len(joined) <= limit:
        return joined
    return joined[:limit] + f"\n... [transcript truncated, {len(joined)} chars total]"
