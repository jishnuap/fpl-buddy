"""The gateway client itself.

One long-lived connection, started as a background task on the same event
loop the API runs on (see ``main.py``) -- there is exactly one always-on
process already (``docs/decisions.md``: "One always-on replica"), so the bot
lives inside it rather than as a second thing to keep running.

``message_content`` is a privileged intent, required so ``on_message`` below
can read the text of ordinary messages in the configured channel and capture
them as notes (see ``../notes.py``). It must be turned on under the bot's
settings in the Discord Developer Portal (Bot tab -> Privileged Gateway
Intents -> MESSAGE CONTENT INTENT), or the bot fails to connect at all.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..config import Settings
from ..orchestrator import Orchestrator
from .views import AmendButton, ApproveButton, RejectButton

logger = logging.getLogger(__name__)

# Acknowledges a captured note without starting a conversation.
NOTE_REACTION = "\U0001f4dd"  # memo


class FplBot(commands.Bot):
    def __init__(self, settings: Settings, orchestrator: Orchestrator | None) -> None:
        # ``orchestrator`` is set after construction when it needs this bot to
        # notify through -- see ``main.py:build_app``. It is always non-None by
        # the time the bot connects and any callback can run.
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.orchestrator: Orchestrator = orchestrator  # type: ignore[assignment]

    async def setup_hook(self) -> None:
        # Registers the custom_id patterns, not any specific message -- this is
        # what makes the buttons survive a restart.
        for item_cls in (ApproveButton, RejectButton, AmendButton):
            self.add_dynamic_items(item_cls)

    async def on_ready(self) -> None:
        logger.info("Discord bot ready as %s.", self.user)

    async def on_message(self, message: discord.Message) -> None:
        """Capture a note from the configured channel; never reply.

        There are no text commands registered on this bot, so this replaces
        (rather than extends) ``commands.Bot``'s default handler -- there is
        nothing for ``process_commands`` to dispatch here.
        """
        if message.author.bot or message.channel.id != self.settings.discord_channel_id:
            return
        text = message.content.strip()
        if not text:
            return
        self.orchestrator.notes.add(author=str(message.author), text=text)
        try:
            await message.add_reaction(NOTE_REACTION)
        except discord.HTTPException:
            logger.warning("Could not react to a captured note (missing permission?).")


def build_bot(settings: Settings, orchestrator: Orchestrator | None = None) -> FplBot:
    return FplBot(settings, orchestrator)
