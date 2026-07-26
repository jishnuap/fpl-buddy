"""Posting to Discord without a gateway connection.

This is the path the cron-driven tick uses. A job that exits after twenty
seconds cannot hold a WebSocket open, but sending a message never needed one --
it is a single authenticated POST.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from fpl_buddy.discord_bot.rest import API_ROOT, DiscordRestNotifier

from .conftest import make_proposal, make_stored

CHANNEL = 987654321
MESSAGES_URL = f"{API_ROOT}/channels/{CHANNEL}/messages"


@pytest.fixture
def rest_settings(settings):
    settings.notify_channel = "discord"
    settings.discord_bot_token = SecretStr("a-bot-token")
    settings.discord_channel_id = CHANNEL
    return settings


@pytest.fixture
def proposal(context):
    return make_stored(make_proposal(), context)


def _posted(route) -> dict:
    return json.loads(route.calls[0].request.content)


def test_it_posts_the_embed_to_the_configured_channel(rest_settings, proposal):
    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200))
        DiscordRestNotifier(rest_settings).notify_proposal(proposal, rest_settings)

    embed = _posted(route)["embeds"][0]
    assert f"GW{proposal.gameweek}" in embed["title"]


def test_the_bot_token_is_sent_as_a_bot_authorization(rest_settings, proposal):
    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200))
        DiscordRestNotifier(rest_settings).notify_proposal(proposal, rest_settings)

    assert route.calls[0].request.headers["authorization"] == "Bot a-bot-token"


def test_the_approval_link_is_in_the_embed(rest_settings, proposal):
    """The gateway version puts this on a button. Without one, the link has to
    be somewhere a thumb can reach it -- otherwise the message is unactionable."""
    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200))
        DiscordRestNotifier(rest_settings).notify_proposal(proposal, rest_settings)

    embed = _posted(route)["embeds"][0]
    review = next(f for f in embed["fields"] if f["name"] == "Review")
    assert "/a/" in review["value"]


def test_no_buttons_are_posted(rest_settings, proposal):
    """A button with nothing listening for the interaction just fails silently
    when tapped, which is worse than not offering one."""
    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200))
        DiscordRestNotifier(rest_settings).notify_proposal(proposal, rest_settings)

    assert "components" not in _posted(route)


def test_a_plain_message_is_clipped_to_discords_limit(rest_settings):
    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200))
        DiscordRestNotifier(rest_settings).send("Subject", "x" * 5000)

    assert len(_posted(route)["content"]) == 2000


def test_a_rejected_post_raises_with_discords_own_reason(rest_settings, proposal):
    """safe_notify swallows this, but the log line has to say what went wrong."""
    with respx.mock:
        respx.post(MESSAGES_URL).mock(
            return_value=httpx.Response(403, text="Missing Access")
        )
        with pytest.raises(RuntimeError, match="403.*Missing Access"):
            DiscordRestNotifier(rest_settings).notify_proposal(proposal, rest_settings)


def test_missing_credentials_fail_at_construction_not_at_send(settings):
    settings.discord_bot_token = SecretStr("")
    settings.discord_channel_id = 0

    with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN"):
        DiscordRestNotifier(settings)


# ------------------------------------------------------------ channel routing
#
# A daily article digest landing in the channel you approve transfers in is how
# a deadline notification gets scrolled past. These pin down which channel wins.

HARVEST_CHANNEL = 111222333
HARVEST_URL = f"{API_ROOT}/channels/{HARVEST_CHANNEL}/messages"


def test_the_harvest_summary_goes_to_its_own_channel(rest_settings):
    rest_settings.discord_harvest_channel_id = HARVEST_CHANNEL
    with respx.mock:
        route = respx.post(HARVEST_URL).mock(return_value=httpx.Response(200))
        DiscordRestNotifier(rest_settings).send(
            "FPL harvest: 2 new articles", "...", meta={"kind": "harvest"}
        )
    assert route.called


def test_proposals_stay_in_the_main_channel_when_harvest_is_split_out(
    rest_settings, proposal
):
    rest_settings.discord_harvest_channel_id = HARVEST_CHANNEL
    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200))
        DiscordRestNotifier(rest_settings).notify_proposal(proposal, rest_settings)
    assert route.called


def test_an_unset_harvest_channel_falls_back_to_the_main_one(rest_settings):
    """Existing setups must keep working without touching their config."""
    rest_settings.discord_harvest_channel_id = 0
    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200))
        DiscordRestNotifier(rest_settings).send("subject", "body", meta={"kind": "harvest"})
    assert route.called


def test_a_message_of_no_particular_kind_goes_to_the_main_channel(rest_settings):
    """Wrong channel is a nuisance; nowhere at all is a lost notification."""
    rest_settings.discord_harvest_channel_id = HARVEST_CHANNEL
    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200))
        DiscordRestNotifier(rest_settings).send("subject", "body")
    assert route.called
