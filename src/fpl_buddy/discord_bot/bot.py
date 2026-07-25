"""The gateway client itself.

One long-lived connection, started as a background task on the same event
loop the API runs on (see ``main.py``) -- there is exactly one always-on
process already (``docs/decisions.md``: "One always-on replica"), so the bot
lives inside it rather than as a second thing to keep running.

No privileged intents are needed yet: buttons and modals arrive as
interactions regardless of intents. ``message_content`` only becomes necessary
once free-form chat is added.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..config import Settings
from ..orchestrator import Orchestrator
from .views import AmendButton, ApproveButton, RejectButton

logger = logging.getLogger(__name__)


class FplBot(commands.Bot):
    def __init__(self, settings: Settings, orchestrator: Orchestrator | None) -> None:
        # ``orchestrator`` is set after construction when it needs this bot to
        # notify through -- see ``main.py:build_app``. It is always non-None by
        # the time the bot connects and any callback can run.
        intents = discord.Intents.default()
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


def build_bot(settings: Settings, orchestrator: Orchestrator | None = None) -> FplBot:
    return FplBot(settings, orchestrator)
