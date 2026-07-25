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
from ..decisions.schema import AgentProposal
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
    agent = build_agent(context, client, settings, model=model)

    brief = context.render()
    prompt = brief if not extra_instruction else f"{brief}\n\n---\n\n{extra_instruction}"

    logger.info("Running agent for GW%s (brief is %d chars).", context.gameweek.id, len(brief))
    result = agent.invoke(
        {"messages": [HumanMessage(content=prompt)]},
        config={"recursion_limit": RECURSION_LIMIT},
    )

    proposal = result.get("structured_response")
    if proposal is None:
        raise AgentConfigError(
            "The agent finished without producing a structured proposal. Last message: "
            f"{_last_text(result)[:500]}"
        )
    if not isinstance(proposal, AgentProposal):
        # A dict comes back when the model's tool call is coerced loosely.
        proposal = AgentProposal.model_validate(proposal)

    return proposal, _transcript(result)


# --------------------------------------------------------------------------- #


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
