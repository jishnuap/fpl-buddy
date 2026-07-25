"""The Discord front end: embeds, buttons, and the sync-to-async bridge.

No gateway connection is ever made here -- ``discord.Embed``/``discord.ui.View``
are plain data structures, and the bridge is tested against a fake bot double
with a real event loop on a background thread, which is the actual mechanism
(``run_coroutine_threadsafe``) that production code relies on.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Iterator

import discord
import pytest

from fpl_buddy.decisions.schema import ProposalStatus
from fpl_buddy.decisions.validate import validate
from fpl_buddy.discord_bot import formatting, views
from fpl_buddy.discord_bot.bot import NOTE_REACTION, build_bot
from fpl_buddy.discord_bot.notifier import DiscordNotifier

from .conftest import FREE_MID_NEW, MID_LIV, make_proposal, make_stored, make_transfer


def stored(context, **overrides):
    return make_stored(make_proposal(), context, **overrides)


# ------------------------------------------------------------------- formatting


def test_embed_title_names_the_gameweek_and_the_headline(context, settings):
    embed = formatting.build_embed(stored(context), settings)
    assert embed.title.startswith("GW4:")
    assert "(C) Vasquez" in embed.title


def test_embed_description_is_the_summary(context, settings):
    embed = formatting.build_embed(stored(context), settings)
    assert embed.description == "Roll the transfer, keep the armband on Vasquez."


def test_embed_shows_no_transfers_as_rolling(context, settings):
    embed = formatting.build_embed(stored(context), settings)
    field = next(f for f in embed.fields if f.name == "Transfers")
    assert field.value == "None (rolling)"


def test_embed_shows_resolved_transfer_prices(context, settings):
    agent = make_proposal(
        transfers=[
            make_transfer(MID_LIV, FREE_MID_NEW, player_out_name="Hollis", player_in_name="Abbott")
        ]
    )
    validate(agent, context, settings)  # resolves the prices, like the real flow does
    embed = formatting.build_embed(make_stored(agent, context), settings)
    field = next(f for f in embed.fields if f.name == "Transfers")
    assert "Hollis -> Abbott" in field.value
    assert "sell £5.2m" in field.value
    assert "buy £5.0m" in field.value


def test_embed_lists_risks(context, settings):
    agent = make_proposal(risks=["Late fitness test on Friday"])
    embed = formatting.build_embed(make_stored(agent, context), settings)
    field = next(f for f in embed.fields if f.name == "Risks")
    assert "Late fitness test on Friday" in field.value


def test_embed_is_red_and_refuses_to_promise_submission_on_fatal_validation(context, settings):
    agent = make_proposal(gameweek=99)
    proposal = make_stored(agent, context, validation_issues=validate(agent, context, settings))
    embed = formatting.build_embed(proposal, settings)
    assert embed.color == discord.Color.red()
    status = next(f for f in embed.fields if f.name == "Status")
    assert "Cannot be submitted" in status.value


def test_embed_is_green_once_executed(context, settings):
    proposal = stored(context, status=ProposalStatus.AUTO_EXECUTED)
    embed = formatting.build_embed(proposal, settings)
    assert embed.color == discord.Color.green()
    status = next(f for f in embed.fields if f.name == "Status")
    assert "Submitted to FPL" in status.value


def test_embed_is_grey_for_a_rejected_proposal(context, settings):
    proposal = stored(context, status=ProposalStatus.REJECTED)
    embed = formatting.build_embed(proposal, settings)
    assert embed.color == discord.Color.greyple()


def test_embed_warns_that_silence_submits(context, settings):
    assert settings.auto_commit_enabled
    embed = formatting.build_embed(stored(context), settings)
    status = next(f for f in embed.fields if f.name == "Status")
    assert "automatically" in status.value


def test_embed_footer_carries_the_proposal_id_and_dry_run_flag(context, settings):
    proposal = stored(context)
    embed = formatting.build_embed(proposal, settings)
    assert proposal.id in embed.footer.text
    assert "DRY_RUN" in embed.footer.text


def test_embed_links_to_the_review_page(context, settings):
    proposal = stored(context)
    embed = formatting.build_embed(proposal, settings)
    assert embed.url.startswith(f"{settings.public_base_url}/a/")


def test_long_fields_are_clipped_not_rejected(context, settings):
    agent = make_proposal(risks=["x" * 2000])
    embed = formatting.build_embed(make_stored(agent, context), settings)
    field = next(f for f in embed.fields if f.name == "Risks")
    assert len(field.value) <= 1024
    assert field.value.endswith("…")


# ----------------------------------------------------------------------- views


def test_button_custom_ids_encode_the_action_and_the_proposal_id():
    assert views.ApproveButton("gw04-abc").custom_id == "fpl:approve:gw04-abc"
    assert views.RejectButton("gw04-abc").custom_id == "fpl:reject:gw04-abc"
    assert views.AmendButton("gw04-abc").custom_id == "fpl:amend:gw04-abc"


def test_proposal_view_has_all_three_buttons():
    view = views.ProposalView("gw04-abc")
    custom_ids = {child.custom_id for child in view.children}
    assert custom_ids == {"fpl:approve:gw04-abc", "fpl:amend:gw04-abc", "fpl:reject:gw04-abc"}


@pytest.mark.parametrize(
    ("pattern", "action"),
    [(views._APPROVE_RE, "approve"), (views._REJECT_RE, "reject"), (views._AMEND_RE, "amend")],
)
def test_custom_id_patterns_only_match_their_own_action(pattern, action):
    match = re.fullmatch(pattern, f"fpl:{action}:gw04-20260101T000000-abc123")
    assert match is not None
    assert match["proposal_id"] == "gw04-20260101T000000-abc123"

    for other in ("approve", "reject", "amend"):
        if other == action:
            continue
        assert re.fullmatch(pattern, f"fpl:{other}:gw04-abc") is None


async def test_from_custom_id_reconstructs_the_button_after_a_restart():
    """This is what makes a button work again after the process restarts --
    Discord replays the custom_id, not any in-memory view."""
    match = re.fullmatch(views._APPROVE_RE, "fpl:approve:gw04-abc")
    button = await views.ApproveButton.from_custom_id(None, None, match)
    assert button.proposal_id == "gw04-abc"
    assert button.custom_id == "fpl:approve:gw04-abc"


def test_result_view_is_none_once_nothing_more_can_be_done(context):
    for status in (ProposalStatus.EXECUTED, ProposalStatus.REJECTED, ProposalStatus.EXPIRED):
        assert views.result_view(stored(context, status=status)) is None


def test_result_view_has_buttons_for_an_open_proposal(context):
    view = views.result_view(stored(context))
    assert view is not None
    assert len(view.children) == 3


def test_result_view_is_none_when_validation_failed(context, settings):
    agent = make_proposal(gameweek=99)
    proposal = make_stored(agent, context, validation_issues=validate(agent, context, settings))
    assert views.result_view(proposal) is None


# -------------------------------------------------------------------- notifier
#
# DiscordNotifier.notify_proposal() is called from whatever thread is running
# the orchestrator (the scheduler's thread, or a FastAPI request thread) and
# has to hand work to the bot's own event loop and block for the result. These
# tests run a *real* event loop on a background thread -- the actual mechanism
# in play, not a mock of it -- with a fake bot standing in for discord.py.


class _FakeChannel:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        return object()


class _FakeBot:
    def __init__(self, loop: asyncio.AbstractEventLoop, channel: _FakeChannel | None) -> None:
        self.loop = loop
        self._channel = channel
        self.fetch_calls = 0

    def get_channel(self, channel_id: int):
        return self._channel

    async def fetch_channel(self, channel_id: int):
        self.fetch_calls += 1
        if self._channel is None:
            raise AssertionError("no channel configured for fetch_channel either")
        return self._channel


@pytest.fixture
def background_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_notify_proposal_crosses_the_thread_boundary_to_the_bots_loop(
    background_loop, context, settings
):
    settings.notify_channel = "discord"
    settings.discord_channel_id = 123
    channel = _FakeChannel()
    bot = _FakeBot(background_loop, channel)

    DiscordNotifier(bot, settings).notify_proposal(stored(context), settings)

    assert len(channel.calls) == 1
    assert isinstance(channel.calls[0]["embed"], discord.Embed)


def test_notify_proposal_falls_back_to_fetch_channel_when_not_cached(
    background_loop, context, settings
):
    settings.notify_channel = "discord"
    settings.discord_channel_id = 123
    channel = _FakeChannel()

    class _UncachedBot(_FakeBot):
        def get_channel(self, channel_id: int):
            return None

    bot = _UncachedBot(background_loop, channel)
    DiscordNotifier(bot, settings).notify_proposal(stored(context), settings)

    assert bot.fetch_calls == 1
    assert len(channel.calls) == 1


def test_notify_proposal_omits_the_view_once_the_proposal_is_terminal(
    background_loop, context, settings
):
    settings.notify_channel = "discord"
    settings.discord_channel_id = 123
    channel = _FakeChannel()
    bot = _FakeBot(background_loop, channel)

    DiscordNotifier(bot, settings).notify_proposal(
        stored(context, status=ProposalStatus.REJECTED), settings
    )

    assert "view" not in channel.calls[0]


# -------------------------------------------------------------- note capture
#
# No conversation, no replies -- ``on_message`` just files anything sent in the
# configured channel away as a note for the next scheduled ``propose()``.


class _FakeNotes:
    def __init__(self) -> None:
        self.added: list[tuple[str, str]] = []

    def add(self, author: str, text: str):
        self.added.append((author, text))
        return object()


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.notes = _FakeNotes()


class _FakeAuthor:
    def __init__(self, *, bot: bool = False, name: str = "jishnu") -> None:
        self.bot = bot
        self._name = name

    def __str__(self) -> str:
        return self._name


class _FakeChannelRef:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id


class _FakeMessage:
    def __init__(self, content: str, *, channel_id: int, author_bot: bool = False) -> None:
        self.content = content
        self.channel = _FakeChannelRef(channel_id)
        self.author = _FakeAuthor(bot=author_bot)
        self.reactions: list[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)


@pytest.fixture
def bot(settings):
    settings.discord_channel_id = 123
    return build_bot(settings, orchestrator=_FakeOrchestrator())


async def test_on_message_captures_a_note_from_the_configured_channel(bot):
    message = _FakeMessage("bench Vasquez, he's got a knock", channel_id=123)
    await bot.on_message(message)
    assert bot.orchestrator.notes.added == [("jishnu", "bench Vasquez, he's got a knock")]
    assert message.reactions == [NOTE_REACTION]


async def test_on_message_ignores_other_channels(bot):
    message = _FakeMessage("hello", channel_id=999)
    await bot.on_message(message)
    assert bot.orchestrator.notes.added == []
    assert message.reactions == []


async def test_on_message_ignores_its_own_messages(bot):
    message = _FakeMessage("GW4: proposal posted", channel_id=123, author_bot=True)
    await bot.on_message(message)
    assert bot.orchestrator.notes.added == []


async def test_on_message_ignores_blank_content(bot):
    message = _FakeMessage("   ", channel_id=123)
    await bot.on_message(message)
    assert bot.orchestrator.notes.added == []
    assert message.reactions == []
