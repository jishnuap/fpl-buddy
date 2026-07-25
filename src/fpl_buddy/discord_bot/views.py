"""Buttons on a proposal message.

Every button is a :class:`discord.ui.DynamicItem`, matched by a regex on its
``custom_id`` rather than tied to an in-memory view instance. That's what lets
a button still work after the bot process restarts mid-approval-window --
the message on Discord's side never changes, only the process reconstructing
its meaning does.

The callbacks call straight into ``Orchestrator`` -- the same methods the web
approval page and the CLI use -- via ``asyncio.to_thread``, because those
calls block on HTTP (and, for amend, an LLM run) and must not freeze the
bot's event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import discord

from ..decisions.executor import ExecutionBlocked
from ..orchestrator import NotActionable, Orchestrator, ProposalNotFound
from .formatting import build_embed

if TYPE_CHECKING:
    from ..config import Settings
    from ..decisions.schema import Proposal
    from .bot import FplBot

logger = logging.getLogger(__name__)

# One pattern per action -- each dynamic item class is registered separately,
# so a shared/generic pattern would make every class match every custom_id.
_APPROVE_RE = r"fpl:approve:(?P<proposal_id>.+)"
_REJECT_RE = r"fpl:reject:(?P<proposal_id>.+)"
_AMEND_RE = r"fpl:amend:(?P<proposal_id>.+)"


def result_view(proposal: Proposal) -> ProposalView | None:
    """Buttons for a proposal, or ``None`` once nothing more can be done to it."""
    if proposal.is_terminal or proposal.fatal_issues:
        return None
    return ProposalView(proposal.id)


async def send_proposal(
    channel: discord.abc.Messageable, embed: discord.Embed, view: ProposalView | None
) -> discord.Message:
    """``channel.send()`` has no overload for an explicit ``view=None`` -- omit the kwarg instead."""
    if view is None:
        return await channel.send(embed=embed)
    return await channel.send(embed=embed, view=view)


async def _report(interaction: discord.Interaction, message: str) -> None:
    try:
        await interaction.followup.send(message, ephemeral=True)
    except discord.HTTPException:
        logger.exception("Could not send the error back to the Discord interaction.")


class _ActionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fpl:noop:(?P<proposal_id>.+)"):
    """Shared plumbing. Never registered itself -- only the concrete subclasses are."""

    action: str
    label: str
    style: discord.ButtonStyle

    def __init__(self, proposal_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label=self.label,
                style=self.style,
                custom_id=f"fpl:{self.action}:{proposal_id}",
            )
        )
        self.proposal_id = proposal_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str]
    ) -> _ActionButton:
        return cls(match["proposal_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError


class ApproveButton(_ActionButton, template=_APPROVE_RE):
    action = "approve"
    label = "Approve"
    style = discord.ButtonStyle.success

    async def callback(self, interaction: discord.Interaction) -> None:
        await _run_and_update(interaction, self.proposal_id, "approve")


class RejectButton(_ActionButton, template=_REJECT_RE):
    action = "reject"
    label = "Reject"
    style = discord.ButtonStyle.danger

    async def callback(self, interaction: discord.Interaction) -> None:
        await _run_and_update(interaction, self.proposal_id, "reject")


class AmendButton(_ActionButton, template=_AMEND_RE):
    action = "amend"
    label = "Amend"
    style = discord.ButtonStyle.secondary

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AmendModal(self.proposal_id))


class ProposalView(discord.ui.View):
    """Posted with a fresh proposal. ``timeout=None``: buttons never expire client-side."""

    def __init__(self, proposal_id: str) -> None:
        super().__init__(timeout=None)
        self.add_item(ApproveButton(proposal_id))
        self.add_item(AmendButton(proposal_id))
        self.add_item(RejectButton(proposal_id))


class AmendModal(discord.ui.Modal, title="Amend this proposal"):
    note: discord.ui.TextInput = discord.ui.TextInput(
        label="What should change?",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. captain Oakley instead, Vasquez is a rotation risk",
        max_length=500,
    )

    def __init__(self, proposal_id: str) -> None:
        super().__init__()
        self.proposal_id = proposal_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot: FplBot = interaction.client  # type: ignore[assignment]
        try:
            revised = await asyncio.to_thread(
                bot.orchestrator.amend, self.proposal_id, str(self.note)
            )
        except ProposalNotFound as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except NotActionable as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        # Amending produces a *new* revision, not an edit of this one -- so the
        # old message is marked superseded in place, and the new revision is a
        # fresh message with its own buttons.
        if interaction.message is not None:
            old_embed = build_embed(bot.orchestrator.get(self.proposal_id) or revised, bot.settings)
            try:
                await interaction.message.edit(embed=old_embed, view=None)
            except discord.HTTPException:
                logger.exception("Could not mark the superseded message as such.")

        new_embed = build_embed(revised, bot.settings)
        await send_proposal(interaction.channel, new_embed, result_view(revised))  # type: ignore[arg-type]
        await interaction.followup.send(
            f"Amended -- see the new proposal above (`{revised.id}`).", ephemeral=True
        )


async def _run_and_update(interaction: discord.Interaction, proposal_id: str, action: str) -> None:
    await interaction.response.defer()
    bot: FplBot = interaction.client  # type: ignore[assignment]
    orchestrator: Orchestrator = bot.orchestrator
    settings: Settings = bot.settings

    try:
        if action == "approve":
            proposal = await asyncio.to_thread(orchestrator.approve, proposal_id)
        else:
            proposal = await asyncio.to_thread(orchestrator.reject, proposal_id)
    except ProposalNotFound as exc:
        await _report(interaction, str(exc))
        return
    except NotActionable as exc:
        await _report(interaction, str(exc))
        return
    except ExecutionBlocked as exc:
        await _report(interaction, f"Re-validation blocked this at submit time: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - report, don't crash the bot's event loop
        logger.exception("Approve/reject failed for %s.", proposal_id)
        await _report(interaction, f"Something went wrong: {exc}")
        return

    embed = build_embed(proposal, settings)
    await interaction.edit_original_response(embed=embed, view=result_view(proposal))
