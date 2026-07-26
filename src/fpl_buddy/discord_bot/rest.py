"""Posting to Discord over plain HTTPS, with no gateway connection.

The gateway bot in :mod:`fpl_buddy.discord_bot.bot` is a WebSocket that has to
stay connected, which is fine in a long-lived container and impossible in a cron
job that exits after twenty seconds. Sending a message, though, has never needed
the gateway -- it is one authenticated POST.

So this notifier is what the tick driver uses: the same embed, rendered by the
same :func:`build_embed`, delivered by HTTP.

What it does *not* do is attach the Approve / Amend / Reject buttons. A button
only works if something is listening for the interaction, and with no gateway
nothing is. Rather than post controls that quietly do nothing, this posts the
embed with the approval link, which is the same one-tap flow email and webhook
users already get. Wiring the buttons back up means registering an interactions
endpoint URL with Discord and verifying its signatures -- a real feature, not a
config change, and deliberately not attempted here.
"""

from __future__ import annotations

import logging

import httpx

from ..approval import review_url
from ..config import Settings
from ..decisions.schema import Proposal
from ..notify import Notifier
from .formatting import build_embed

logger = logging.getLogger(__name__)

API_ROOT = "https://discord.com/api/v10"

# Discord rejects a message over 2000 characters outright.
_CONTENT_LIMIT = 2000


class DiscordRestNotifier(Notifier):
    def __init__(self, settings: Settings) -> None:
        if not settings.has_discord:
            raise RuntimeError(
                "Posting to Discord needs both DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID."
            )
        self.settings = settings

    # ------------------------------------------------------------------ send
    def send(self, subject: str, text: str, *, html: str | None = None, meta: dict | None = None) -> None:
        body = f"**{subject}**\n{text}"
        self._post({"content": body[:_CONTENT_LIMIT]})

    def notify_proposal(self, proposal: Proposal, settings: Settings) -> None:
        embed = build_embed(proposal, settings).to_dict()
        # The gateway version puts this on a button. Without one, the link has
        # to be somewhere a thumb can reach it.
        embed.setdefault("fields", []).append(
            {
                "name": "Review",
                "value": f"[Approve, amend or reject]({review_url(settings, proposal.id)})",
                "inline": False,
            }
        )
        self._post({"embeds": [embed]})

    # --------------------------------------------------------------- private
    def _post(self, payload: dict) -> None:
        url = f"{API_ROOT}/channels/{self.settings.discord_channel_id}/messages"
        with httpx.Client(timeout=self.settings.http_timeout_seconds) as client:
            response = client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bot {self.settings.discord_bot_token.get_secret_value()}",
                    "User-Agent": "fpl-buddy (https://github.com/jishnuap/fpl-buddy, 0.1.0)",
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Discord returned {response.status_code}: {response.text[:200]}"
            )
        logger.info("Posted to Discord channel %s.", self.settings.discord_channel_id)
