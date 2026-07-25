"""Agent wiring, driven by a fake chat model so nothing reaches Azure.

What matters here is not the model's judgement -- it's that the graph is wired
such that a run ends in a typed ``AgentProposal``, that the toolset the model
gets is read-only, and that a missing Azure config fails loudly at build time
rather than at 45 minutes before the deadline.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from fpl_buddy.agent.build import AgentConfigError, build_agent, build_model, run_agent
from fpl_buddy.agent.subagents import build_subagents
from fpl_buddy.agent.tools import build_tools
from fpl_buddy.decisions.schema import AgentProposal
from fpl_buddy.decisions.validate import validate

from .conftest import FREE_MID_NEW, FWD_CAPTAIN, NEXT_GAMEWEEK
from .fakes import ONE_TRANSFER_PROPOSAL, FakeStructuredModel
from .fakes import tool_name as _tool_name

# Any tool name suggesting a write would be a bug; assert on it explicitly.
FORBIDDEN_SUBSTRINGS = ("transfer_submit", "submit", "post", "buy", "sell", "set_captain", "write")


def transferring_model() -> FakeStructuredModel:
    return FakeStructuredModel(payload=ONE_TRANSFER_PROPOSAL)


class DummyClient:
    """Stands in for FPLClient. Any network call here is a test failure."""

    def player_summary(self, element_id: int) -> dict:
        raise AssertionError("player_detail should not be reached in these tests")


# ------------------------------------------------------------------ the toolset


def test_toolset_is_read_only(context):
    names = [_tool_name(t) for t in build_tools(context, DummyClient())]
    assert names, "expected some tools"
    for name in names:
        assert not any(bad in name.lower() for bad in FORBIDDEN_SUBSTRINGS), name


def test_tools_cover_the_lookups_the_prompt_promises(context):
    names = {_tool_name(t) for t in build_tools(context, DummyClient())}
    assert {"inspect_squad", "find_player", "candidates", "projections"} <= names


def test_find_player_returns_ids_not_guesses(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["find_player"].invoke({"name": "Vasquez"})
    assert f"id={FWD_CAPTAIN}" in out


def test_find_player_says_so_when_nothing_matches(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "No player matching" in tools["find_player"].invoke({"name": "Zlatan"})


def test_candidates_excludes_flagged_players(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["candidates"].invoke({"position": "FWD", "max_price": 15.0, "limit": 25})
    assert "id=640" not in out, "unavailable player must not be offered"
    assert "id=641" not in out, "doubtful player must not be offered"


def test_candidates_respects_the_price_ceiling(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["candidates"].invoke({"position": "FWD", "max_price": 7.0, "limit": 25})
    assert f"id={FWD_CAPTAIN}" not in out, "£14.5m forward is over a £7.0m ceiling"


def test_candidates_rejects_a_bad_position(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "must be one of" in tools["candidates"].invoke({"position": "STRIKER"})


def test_projections_degrade_gracefully_without_solio(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "unavailable" in tools["projections"].invoke({"board": "topProjected"}).lower()


def test_projections_read_a_board_when_solio_is_present(context, solio):
    context.solio = solio
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["projections"].invoke({"board": "topCaptains", "limit": 5})
    assert "topCaptains" in out
    assert f"id={FWD_CAPTAIN}" in out


def test_projections_rejects_an_unknown_board(context, solio):
    context.solio = solio
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "Unknown board" in tools["projections"].invoke({"board": "topVibes"})


def test_club_fixtures_accepts_a_code_and_rejects_nonsense(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "Man City" in tools["club_fixtures"].invoke({"club": "MCI"})
    assert "Unknown club" in tools["club_fixtures"].invoke({"club": "Real Madrid"})


def test_player_detail_refuses_an_unknown_id_without_calling_out(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "does not exist" in tools["player_detail"].invoke({"element_id": 999_999})


def test_squad_rules_states_the_live_budget(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["squad_rules"].invoke({})
    assert f"£{context.my_team.bank_millions:.1f}m" in out
    assert f"Free transfers: {context.my_team.free_transfers}" in out


# ----------------------------------------------------------------- subagents


def test_subagents_share_the_read_only_toolset(context):
    tools = build_tools(context, DummyClient())
    subs = build_subagents(tools)
    assert {s["name"] for s in subs} == {"captaincy-specialist", "transfer-scout"}
    for sub in subs:
        assert sub["tools"] == tools
        assert sub["description"] and sub["system_prompt"]


# --------------------------------------------------------------- graph wiring


def test_run_agent_returns_a_typed_proposal(context, settings):
    model = transferring_model()
    proposal, transcript = run_agent(context, DummyClient(), settings, model=model)

    assert isinstance(proposal, AgentProposal)
    assert proposal.gameweek == NEXT_GAMEWEEK
    assert proposal.captaincy.captain_id == FWD_CAPTAIN
    assert [t.element_in for t in proposal.transfers] == [FREE_MID_NEW]
    assert isinstance(transcript, str)


def test_the_agents_own_proposal_passes_the_guardrails(context, settings):
    """End to end on the safe path: agent output -> validate() -> no issues."""
    proposal, _ = run_agent(context, DummyClient(), settings, model=transferring_model())
    assert validate(proposal, context, settings) == []


def test_the_brief_is_what_gets_sent_to_the_model(context, settings):
    model = FakeStructuredModel()
    run_agent(context, DummyClient(), settings, model=model)
    assert model.calls >= 1
    assert any("FPL decision brief" in prompt for prompt in model.seen_prompts)
    assert any("Your squad" in prompt for prompt in model.seen_prompts)


def test_amendment_instruction_is_appended_to_the_brief(context, settings):
    model = FakeStructuredModel()
    run_agent(
        context, DummyClient(), settings, extra_instruction="Captain Oakley instead.", model=model
    )
    assert any("Captain Oakley instead." in text for text in model.seen_prompts)
    assert any("FPL decision brief" in text for text in model.seen_prompts)


def test_agent_without_a_structured_response_is_an_error(context, settings):
    class Mute(FakeStructuredModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="I'd rather not."))]
            )

    with pytest.raises(AgentConfigError, match="without producing a structured proposal"):
        run_agent(context, DummyClient(), settings, model=Mute())


def test_build_agent_does_not_need_azure_when_a_model_is_injected(context, settings):
    assert build_agent(context, DummyClient(), settings, model=FakeStructuredModel()) is not None


# ------------------------------------------------------------- model config


def test_missing_endpoint_fails_at_build_time(settings):
    settings.azure_openai_endpoint = ""
    with pytest.raises(AgentConfigError, match="AZURE_OPENAI_ENDPOINT"):
        build_model(settings)


def test_missing_key_fails_at_build_time(settings):
    settings.azure_openai_endpoint = "https://example.openai.azure.com"
    with pytest.raises(AgentConfigError, match="AZURE_OPENAI_API_KEY"):
        build_model(settings)


def test_key_auth_configures_the_deployment(settings):
    from pydantic import SecretStr

    settings.azure_openai_endpoint = "https://example.openai.azure.com"
    settings.azure_openai_api_key = SecretStr("sk-test")
    settings.azure_openai_deployment = "gpt-4.1"
    settings.azure_openai_api_version = "2024-10-21"

    model = build_model(settings)
    assert model.deployment_name == "gpt-4.1"
    assert model.openai_api_version == "2024-10-21"


def test_managed_identity_path_uses_a_token_provider(settings, monkeypatch):
    settings.azure_openai_endpoint = "https://example.openai.azure.com"
    settings.azure_openai_auth = "managed_identity"

    called = {}

    def fake_provider(credential, scope):
        called["scope"] = scope
        return lambda: "fake-token"

    import fpl_buddy.agent.build as build_module

    fake_identity = type(
        "azure.identity",
        (),
        {
            "DefaultAzureCredential": lambda *a, **k: object(),
            "get_bearer_token_provider": staticmethod(fake_provider),
        },
    )
    monkeypatch.setitem(
        __import__("sys").modules, "azure.identity", fake_identity
    )
    monkeypatch.setattr(build_module, "COGNITIVE_SERVICES_SCOPE", "scope/.default")

    model = build_model(settings)
    assert called["scope"] == "scope/.default"
    assert model.azure_ad_token_provider is not None
