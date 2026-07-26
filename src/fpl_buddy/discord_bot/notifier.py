"""Bridges the (synchronous) ``Notifier`` interface onto the bot's asyncio loop.

``notify_proposal`` is called from whatever thread happens to be running the
orchestrator -- the scheduler's own thread, or FastAPI's threadpool for a sync
endpoint -- never from the bot's event loop itself. ``run_coroutine_threadsafe``
is the documented way to hand work to a loop running on another thread and
block the caller until it finishes, which matches how every other Notifier
already behaves (``send`` is synchronous throughout this codebase).
"""

from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..decisions.schema import Proposal
from ..notify import Notifier
from .formatting import build_embed
from .views import result_view, send_proposal

logger = logging.getLogger(__name__)

# The bot's loop is on the same process as the caller; this is generous
# headroom for a Discord API round-trip, not a network timeout tuned in.
SEND_TIMEOUT_SECONDS = 15.0


class DiscordNotifier(Notifier):
    def __init__(self, bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings

    def send(self, subject: str, text: str, *, html: str | None = None, meta: dict | None = None) -> None:
        """Plain-text fallback, and the path the harvest summary takes.

        ``notify_proposal`` is the real path for proposals -- it posts a
        formatted embed with buttons instead of calling this.
        """
        channel_id = self.settings.discord_channel_for((meta or {}).get("kind", ""))
        future = asyncio.run_coroutine_threadsafe(
            self._send_text(subject, text, channel_id), self.bot.loop
        )
        future.result(timeout=SEND_TIMEOUT_SECONDS)

    def notify_proposal(self, proposal: Proposal, settings: Settings) -> None:
        future = asyncio.run_coroutine_threadsafe(self._post(proposal), self.bot.loop)
        future.result(timeout=SEND_TIMEOUT_SECONDS)

    async def _post(self, proposal: Proposal) -> None:
        channel = await self._channel(self.settings.discord_channel_id)
        embed = build_embed(proposal, self.settings)
        await send_proposal(channel, embed, result_view(proposal))

    async def _send_text(self, subject: str, text: str, channel_id: int) -> None:
        channel = await self._channel(channel_id)
        await channel.send(f"**{subject}**\n{text}")

    async def _channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        return channel
