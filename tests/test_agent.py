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

from .conftest import (
    DEF_INJURED,
    FREE_MID_ARS,
    FREE_MID_NEW,
    FWD_CAPTAIN,
    GK_STARTER,
    MID_LIV,
    NEXT_GAMEWEEK,
)
from .fakes import (
    CAPTAIN_NOT_OWNED_PROPOSAL,
    ONE_TRANSFER_PROPOSAL,
    OVER_BUDGET_PROPOSAL,
    FakeChangingModel,
    FakeStructuredModel,
)
from .fakes import tool_name as _tool_name

# Any tool name suggesting a write would be a bug; assert on it explicitly.
FORBIDDEN_SUBSTRINGS = ("transfer_submit", "submit", "post", "buy", "sell", "set_captain", "write")


def transferring_model() -> FakeStructuredModel:
    return FakeStructuredModel(payload=ONE_TRANSFER_PROPOSAL)


class DummyClient:
    """Stands in for FPLClient. Any network call here is a test failure."""

    def __init__(self, *, notes_error: Exception | None = None) -> None:
        self.notes_error = notes_error
        self.notes_calls = 0

    def player_summary(self, element_id: int) -> dict:
        raise AssertionError("player_detail should not be reached in these tests")

    def set_piece_notes(self) -> dict:
        self.notes_calls += 1
        if self.notes_error is not None:
            raise self.notes_error
        return {
            "teams": [
                {"id": 1, "notes": [{"info_message": "Corrigan has taken the last three pens"}]},
                {"id": 2, "notes": [{"info_message": "Check back for additional notes soon"}]},
            ]
        }


# ------------------------------------------------------------------ the toolset


def test_toolset_is_read_only(context):
    names = [_tool_name(t) for t in build_tools(context, DummyClient())]
    assert names, "expected some tools"
    for name in names:
        assert not any(bad in name.lower() for bad in FORBIDDEN_SUBSTRINGS), name


def test_tools_cover_the_lookups_the_prompt_promises(context):
    names = {_tool_name(t) for t in build_tools(context, DummyClient())}
    assert {
        "inspect_squad",
        "find_player",
        "candidates",
        "projections",
        "transfer_options",
        "set_piece_takers",
        "underlying_stats",
    } <= names


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


def test_club_fixtures_spans_the_whole_horizon_not_just_this_gameweek(context):
    """The scout is told to judge a transfer over several gameweeks -- this is
    the tool that has to be able to answer that."""
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["club_fixtures"].invoke({"club": "LIV"})
    assert "GW4" in out and "GW5" in out
    assert "this gameweek only" not in out


def test_club_fixtures_falls_back_to_this_gameweek_without_a_horizon(context):
    context.horizon_fixtures = []
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["club_fixtures"].invoke({"club": "LIV"})
    assert "GW4" in out


# ------------------------------------------------------------ transfer options


def test_transfer_options_refuses_a_player_you_do_not_own(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["transfer_options"].invoke({"element_out": FREE_MID_NEW})
    assert "not in your squad" in out


def test_transfer_options_budget_is_bank_plus_the_selling_price(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["transfer_options"].invoke({"element_out": MID_LIV})
    # bank £1.5m + selling price £5.2m (below now_cost, per the sell-on rule)
    assert "£5.2m" in out
    assert "£6.7m" in out


def test_transfer_options_excludes_the_unaffordable(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["transfer_options"].invoke({"element_out": MID_LIV, "limit": 25})
    assert "id=634" not in out, "£7.5m midfielder is over a £6.7m budget"


def test_transfer_options_respects_the_club_limit(context):
    """Selling frees a slot at the outgoing player's club, and only there."""
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["transfer_options"].invoke({"element_out": MID_LIV, "limit": 25})

    assert f"id={FREE_MID_ARS}" not in out, "Arsenal already has three; a fourth is illegal"
    assert f"id={FREE_MID_NEW}" in out, "Newcastle has none, so they are legal"


def test_transfer_options_offers_only_the_same_position(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["transfer_options"].invoke({"element_out": MID_LIV, "limit": 25})
    for line in out.splitlines()[1:]:
        assert ", MID)" in line, line


def test_transfer_options_excludes_flagged_and_owned_players(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["transfer_options"].invoke({"element_out": FWD_CAPTAIN, "limit": 25})
    assert "id=640" not in out, "unavailable"
    assert "id=641" not in out, "doubtful"
    for pick in context.my_team.picks:
        assert f"id={pick.element}" not in out, "already owned"


def test_transfer_options_says_so_when_nothing_is_affordable(context):
    context.my_team.bank = 0
    for pick in context.my_team.picks:
        if pick.element == GK_STARTER:
            pick.selling_price = 1  # £0.1m buys nothing
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["transfer_options"].invoke({"element_out": GK_STARTER})
    assert "no available GKP fits" in out


# ------------------------------------------------------------------ set pieces


def test_set_piece_takers_lists_the_order_with_ids(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["set_piece_takers"].invoke({"club": "MCI"})
    assert "Penalties: 1." in out
    assert f"id={FWD_CAPTAIN}" in out


def test_set_piece_takers_includes_official_notes_but_not_the_placeholder(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "last three pens" in tools["set_piece_takers"].invoke({"club": "ARS"})
    # Team 2's note is FPL's "check back" placeholder, which is not information.
    assert "Official notes" not in tools["set_piece_takers"].invoke({"club": "MCI"})


def test_set_piece_notes_are_fetched_once_per_run(context):
    client = DummyClient()
    tools = {_tool_name(t): t for t in build_tools(context, client)}
    tools["set_piece_takers"].invoke({"club": "ARS"})
    tools["set_piece_takers"].invoke({"club": "MCI"})
    assert client.notes_calls == 1, "the notes payload should be cached for the run"


def test_set_piece_takers_survives_the_notes_endpoint_failing(context):
    import httpx

    client = DummyClient(notes_error=httpx.ConnectError("down"))
    tools = {_tool_name(t): t for t in build_tools(context, client)}
    out = tools["set_piece_takers"].invoke({"club": "MCI"})
    assert "Penalties: 1." in out, "bootstrap order still works without the notes"


def test_set_piece_takers_rejects_an_unknown_club(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "Unknown club" in tools["set_piece_takers"].invoke({"club": "Real Madrid"})


# ------------------------------------------------------------ underlying stats


def test_underlying_stats_reports_the_opta_numbers(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["underlying_stats"].invoke({"element_id": FWD_CAPTAIN})
    assert "xG/90 0.45" in out
    assert "xGI/90 0.67" in out
    assert "starts/90 1.00" in out
    assert "P1" in out, "set-piece duty"


def test_underlying_stats_refuses_an_unknown_id(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "does not exist" in tools["underlying_stats"].invoke({"element_id": 999_999})


def test_underlying_stats_breaks_the_solio_projection_down(context, solio):
    """The total hides which bet you are making.

    Solio sends the decomposition and it used to be parsed and dropped. Two
    players projected the same are not the same captaincy case if one gets
    there on goals and the other on bonus.
    """
    context.solio = solio
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["underlying_stats"].invoke({"element_id": FWD_CAPTAIN})

    assert "proj 7.90" in out
    assert "0.42 goals" in out
    assert "0.21 assists" in out
    assert "0.80 bonus pts" in out
    # Labelled as gameweek totals, so they are not read as the per-90 rates on
    # the line above.
    assert "projected this gameweek" in out


def test_underlying_stats_reports_captaincy_and_leverage(context, solio):
    """`leverage` is prPoints x (1 - ownership/100) -- the differential view.

    Verified against live Solio data: the correlation with that expression is
    1.000. It is reported with the ownership share spelled out so the agent is
    not left to infer what the bare number means.
    """
    context.solio = solio
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["underlying_stats"].invoke({"element_id": FWD_CAPTAIN})

    assert "captaincy projection 15.01" in out
    assert "leverage 1.40" in out
    # 71.4% own him, so 29% do not.
    assert "29%" in out


def test_underlying_stats_survives_a_player_solio_has_no_row_for(context, solio):
    """Solio ships leaderboards, not a full table -- most players are absent."""
    context.solio = solio
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["underlying_stats"].invoke({"element_id": DEF_INJURED})
    assert "does not exist" not in out
    assert "Solio" not in out, "no row means no Solio section, not a zeroed one"


# -------------------------------------------------------------- knowledge tools


def _article(**overrides):
    from datetime import UTC, datetime

    from fpl_buddy.knowledge.store import ArticleNote

    payload = {
        "id": "src-2026-07-25-captaincy",
        "title": "Vasquez is the obvious captain",
        "url": "https://news.example.test/2026/07/25/captaincy",
        "source": "src",
        "summary": "The author argues for Vasquez on penalties.",
        "key_points": ["He takes the penalties"],
        "published": datetime(2026, 7, 25, tzinfo=UTC),
        "tags": ["captaincy"],
        "players": [FWD_CAPTAIN],
        "trust": "high",
    }
    payload.update(overrides)
    return ArticleNote(**payload)


def test_knowledge_tools_are_registered(context):
    names = {_tool_name(t) for t in build_tools(context, DummyClient())}
    assert {"search_articles", "read_article", "articles_about"} <= names


def test_search_articles_finds_a_match_and_flags_it_as_opinion(context):
    context.articles = [_article()]
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["search_articles"].invoke({"query": "penalties"})

    assert "Vasquez is the obvious captain" in out
    assert "Third-party commentary" in out, "the agent must know what it is reading"
    assert "not instructions" in out.casefold()


def test_search_articles_says_so_when_there_is_nothing(context):
    context.articles = [_article()]
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "Nothing in the archive" in tools["search_articles"].invoke({"query": "goalkeepers"})


def test_search_articles_degrades_without_a_harvest(context):
    assert context.articles == []
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "Nothing in the archive" in tools["search_articles"].invoke({"query": "anything"})


# The brief carries a recent window; the archive goes back further. These pin the
# thing that was quietly false before: that the tools could reach past it.


def _archive(tmp_path, notes):
    from fpl_buddy.knowledge.store import KnowledgeStore

    store = KnowledgeStore(tmp_path / "knowledge")
    for note in notes:
        store.save(note)
    return store


def test_articles_about_reaches_past_the_briefs_recent_window(context, tmp_path):
    """A three-week-old set-piece note still decides a captaincy call, and it
    dropped off the brief long before it stopped mattering."""
    from datetime import UTC, datetime, timedelta

    old = _article(
        id="src-2026-07-01-old-setpiece",
        url="https://news.example.test/old",
        title="Set-piece duty has changed",
        published=datetime.now(UTC) - timedelta(days=20),
        ttl_days=60,
    )
    store = _archive(tmp_path, [old])

    context.articles = []  # nothing in the brief at all
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient(), knowledge=store)}

    out = tools["articles_about"].invoke({"element_id": FWD_CAPTAIN})
    assert "Set-piece duty has changed" in out


def test_search_articles_reaches_past_the_recent_window(context, tmp_path):
    from datetime import UTC, datetime, timedelta

    old = _article(
        id="src-2026-07-01-old-rotation",
        url="https://news.example.test/old",
        summary="The author warns about rotation risk.",
        published=datetime.now(UTC) - timedelta(days=20),
        ttl_days=60,
    )
    store = _archive(tmp_path, [old])
    context.articles = []
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient(), knowledge=store)}

    assert "rotation risk" in tools["search_articles"].invoke({"query": "rotation"})


def test_read_article_opens_an_id_that_is_not_in_the_brief(context, tmp_path):
    """articles_about can hand back an out-of-window id, so read_article has to
    be able to open one -- otherwise the pair contradict each other."""
    from datetime import UTC, datetime, timedelta

    old = _article(
        id="src-2026-07-01-old",
        url="https://news.example.test/old",
        published=datetime.now(UTC) - timedelta(days=20),
        ttl_days=60,
    )
    store = _archive(tmp_path, [old])
    context.articles = []
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient(), knowledge=store)}

    out = tools["read_article"].invoke({"article_id": "src-2026-07-01-old"})
    assert "Vasquez" in out
    assert "No article with id" not in out


def test_expired_notes_stay_out_of_reach(context, tmp_path):
    """Past the window is fine; past its TTL is not. Stale team news is worse
    than none."""
    from datetime import UTC, datetime, timedelta

    stale = _article(
        id="src-2026-06-01-stale",
        url="https://news.example.test/stale",
        published=datetime.now(UTC) - timedelta(days=90),
        ttl_days=21,
    )
    store = _archive(tmp_path, [stale])
    context.articles = []
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient(), knowledge=store)}

    assert "No harvested article mentions" in tools["articles_about"].invoke(
        {"element_id": FWD_CAPTAIN}
    )


def test_without_an_archive_the_tools_use_the_brief(context):
    """No harvest configured must behave exactly as it did before."""
    context.articles = [_article()]
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient(), knowledge=None)}

    assert "obvious captain" in tools["search_articles"].invoke({"query": "penalties"})
    assert "obvious captain" in tools["articles_about"].invoke({"element_id": FWD_CAPTAIN})
    assert "argues for Vasquez" in tools["read_article"].invoke(
        {"article_id": "src-2026-07-25-captaincy"}
    )


def test_the_brief_tells_the_agent_the_archive_is_bigger_than_the_list(context):
    context.articles = [_article()]
    brief = context.render()
    assert "archive goes back further" in brief
    assert "search_articles" in brief and "articles_about" in brief


def test_read_article_returns_the_summary_and_claims(context):
    context.articles = [_article()]
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["read_article"].invoke({"article_id": "src-2026-07-25-captaincy"})

    assert "argues for Vasquez" in out
    assert "He takes the penalties" in out
    assert "trust: high" in out


def test_read_article_rejects_an_unknown_id_and_lists_real_ones(context):
    context.articles = [_article()]
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["read_article"].invoke({"article_id": "made-up"})
    assert "No article with id" in out
    assert "src-2026-07-25-captaincy" in out


def test_read_article_marks_a_paywalled_one_as_partial(context):
    context.articles = [_article(access="partial")]
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    out = tools["read_article"].invoke({"article_id": "src-2026-07-25-captaincy"})
    assert "PARTIAL" in out


def test_articles_about_a_player_uses_resolved_ids(context):
    context.articles = [_article()]
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}

    assert "obvious captain" in tools["articles_about"].invoke({"element_id": FWD_CAPTAIN})
    assert "No harvested article" in tools["articles_about"].invoke({"element_id": MID_LIV})


def test_articles_about_refuses_an_unknown_player(context):
    tools = {_tool_name(t): t for t in build_tools(context, DummyClient())}
    assert "does not exist" in tools["articles_about"].invoke({"element_id": 999_999})


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


# --------------------------------------------------------------- repair loop
#
# The failure these cover happened for real: the agent captained the top row of
# a league-wide projection board, and separately started a player it had just
# transferred out. Both were caught by validate() and the human got an
# unsubmittable proposal. Prompt wording already forbade both, so the fix is a
# loop that hands the guardrail errors back rather than more words.


def test_a_proposal_failing_the_guardrails_is_handed_back_for_repair(context, settings):
    model = FakeChangingModel(payloads=[CAPTAIN_NOT_OWNED_PROPOSAL, ONE_TRANSFER_PROPOSAL])
    proposal, _ = run_agent(context, DummyClient(), settings, model=model)

    assert validate(proposal, context, settings) == []
    assert proposal.captaincy.captain_id == FWD_CAPTAIN


def test_the_repair_message_names_the_actual_failure(context, settings):
    model = FakeChangingModel(payloads=[CAPTAIN_NOT_OWNED_PROPOSAL, ONE_TRANSFER_PROPOSAL])
    run_agent(context, DummyClient(), settings, model=model)

    repair = next(p for p in model.seen_prompts if "failed the deterministic guardrails" in p)
    assert f"id {FREE_MID_NEW}) is not in the resulting squad" in repair


def test_the_repair_message_spells_out_the_post_transfer_squad(context, settings):
    """The agent's arithmetic is what failed, so hand it the answer, not the rule."""
    model = FakeChangingModel(payloads=[CAPTAIN_NOT_OWNED_PROPOSAL, ONE_TRANSFER_PROPOSAL])
    run_agent(context, DummyClient(), settings, model=model)

    repair = next(p for p in model.seen_prompts if "failed the deterministic guardrails" in p)
    # The bad proposal transfers nobody, so the resulting squad is the current one.
    for element_id in context.owned_element_ids():
        assert f"id={element_id}" in repair
    assert f"id={FREE_MID_NEW} " not in repair


def test_an_overspend_is_sent_back_with_the_sums_not_just_the_verdict(context, settings):
    """First live run repeated the same overspend three times against a squad-only
    repair message. Quoting "short by £4.0m" back is not enough to act on."""
    model = FakeChangingModel(payloads=[OVER_BUDGET_PROPOSAL, ONE_TRANSFER_PROPOSAL])
    run_agent(context, DummyClient(), settings, model=model)

    repair = next(p for p in model.seen_prompts if "failed the deterministic guardrails" in p)
    assert "budget across ALL the transfers together" in repair
    assert "bank" in repair
    assert "+ sell" in repair and "- buy" in repair
    assert "OVERSPENT" in repair


def test_no_budget_block_when_nothing_is_being_bought(context, settings):
    model = FakeChangingModel(payloads=[CAPTAIN_NOT_OWNED_PROPOSAL, ONE_TRANSFER_PROPOSAL])
    run_agent(context, DummyClient(), settings, model=model)

    repair = next(p for p in model.seen_prompts if "failed the deterministic guardrails" in p)
    assert "budget across ALL the transfers" not in repair


def test_a_valid_proposal_is_never_sent_back(context, settings):
    model = FakeChangingModel(payloads=[ONE_TRANSFER_PROPOSAL])
    run_agent(context, DummyClient(), settings, model=model)

    assert not any("failed the deterministic guardrails" in p for p in model.seen_prompts)


def test_an_unfixable_proposal_still_reaches_the_human_with_its_issues(context, settings):
    """Raising would show the human nothing. A flagged bad proposal beats silence."""
    settings.agent_repair_attempts = 1
    model = FakeChangingModel(payloads=[CAPTAIN_NOT_OWNED_PROPOSAL])

    proposal, _ = run_agent(context, DummyClient(), settings, model=model)

    assert proposal.captaincy.captain_id == FREE_MID_NEW
    assert [i.code for i in validate(proposal, context, settings)] == ["captain_not_in_squad"]


def test_repair_attempts_can_be_switched_off(context, settings):
    settings.agent_repair_attempts = 0
    model = FakeChangingModel(payloads=[CAPTAIN_NOT_OWNED_PROPOSAL, ONE_TRANSFER_PROPOSAL])

    proposal, _ = run_agent(context, DummyClient(), settings, model=model)

    assert proposal.captaincy.captain_id == FREE_MID_NEW
    assert not any("failed the deterministic guardrails" in p for p in model.seen_prompts)


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


# ----------------------------------------------------- picking a provider


def test_the_provider_setting_decides_which_client_is_built(settings):
    """Azure and Google are both maintained paths, chosen by one variable.

    A disabled Azure resource is not a hypothetical -- it is what prompted this
    -- and neither is a rate-limited Google key, so switching has to be a
    setting rather than a different build.
    """
    from pydantic import SecretStr

    settings.llm_provider = "google"
    settings.google_api_key = SecretStr("ai-studio-test")

    model = build_model(settings)
    assert type(model).__name__ == "ChatGoogleGenerativeAI"

    settings.llm_provider = "azure"
    settings.azure_openai_endpoint = "https://example.openai.azure.com"
    settings.azure_openai_api_key = SecretStr("sk-test")
    assert type(build_model(settings)).__name__ == "AzureChatOpenAI"


def test_google_needs_a_key(settings):
    settings.llm_provider = "google"
    with pytest.raises(AgentConfigError, match="GOOGLE_API_KEY"):
        build_model(settings)


def test_the_harvest_summarises_with_the_cheaper_google_model(settings):
    """A dozen schema extractions in a burst; the RPM ceiling is the constraint.

    Both roles resolve to one deployment on Azure, where a second deployment
    would have to be provisioned to say anything different.
    """
    from pydantic import SecretStr

    settings.llm_provider = "google"
    settings.google_api_key = SecretStr("ai-studio-test")
    settings.google_model = "gemini-3.8-flash"
    settings.google_summary_model = "gemini-3.8-flash-lite"

    assert "gemini-3.8-flash" in build_model(settings, role="agent").model
    assert "gemini-3.8-flash-lite" in build_model(settings, role="summary").model


def test_azure_ignores_the_role_because_a_deployment_is_one_model(settings):
    from pydantic import SecretStr

    settings.llm_provider = "azure"
    settings.azure_openai_endpoint = "https://example.openai.azure.com"
    settings.azure_openai_api_key = SecretStr("sk-test")
    settings.azure_openai_deployment = "gpt-4.1"

    for role in ("agent", "summary"):
        assert build_model(settings, role=role).deployment_name == "gpt-4.1"
