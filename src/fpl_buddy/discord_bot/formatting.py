"""Turn a ``Proposal`` into a Discord embed.

Kept separate from the ``discord.py`` client so the layout is testable without
a gateway connection: :func:`build_embed` takes plain data in, returns a
``discord.Embed`` out, no network or event loop involved.
"""

from __future__ import annotations

import discord

from ..approval import review_url
from ..config import Settings
from ..decisions.schema import Proposal, ProposalStatus

# Discord hard limits (embeds.md): field value 1024, footer 2048.
_FIELD_LIMIT = 1024


def _clip(text: str, limit: int = _FIELD_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _color(proposal: Proposal) -> discord.Color:
    if proposal.fatal_issues or proposal.is_stale:
        return discord.Color.red()
    if proposal.status == ProposalStatus.FAILED:
        return discord.Color.red()
    if proposal.status in (ProposalStatus.EXECUTED, ProposalStatus.AUTO_EXECUTED):
        return discord.Color.green()
    if proposal.is_terminal:
        return discord.Color.greyple()
    return discord.Color.gold()


def _status_line(proposal: Proposal, settings: Settings) -> str:
    if proposal.is_stale:
        # Reads identically to a fresh plan otherwise, which is the whole danger:
        # the names in it can belong to a squad that has since been rebuilt.
        return (
            f"**Written {proposal.describe_lead_time()} before this deadline and never "
            "re-derived.** It has not seen the current squad or the team news -- check "
            "every name before acting."
        )
    if proposal.fatal_issues:
        return "**Cannot be submitted -- failed validation.** Amend it or act in the app."
    if proposal.status in (ProposalStatus.EXECUTED, ProposalStatus.AUTO_EXECUTED):
        note = " (DRY_RUN -- nothing really went out)" if settings.dry_run else ""
        return f"Submitted to FPL.{note}"
    if proposal.status == ProposalStatus.FAILED:
        return "**Submission failed.** Nothing is retried automatically -- check the app."
    if proposal.is_terminal:
        return f"Status: {proposal.status.value}. Nothing further will happen."
    if settings.auto_commit_enabled:
        minutes = settings.commit_minutes_before_deadline
        return f"Doing nothing submits this automatically ~{minutes:.0f} min before the deadline."
    return "Auto-commit is off: without approval, nothing is submitted."


def build_embed(proposal: Proposal, settings: Settings) -> discord.Embed:
    agent = proposal.agent
    deadline = proposal.deadline.astimezone().strftime("%a %d %b %H:%M %Z")
    prefix = "[STALE] " if proposal.is_stale else ""

    embed = discord.Embed(
        title=_clip(f"{prefix}GW{proposal.gameweek}: {proposal.headline()}", 256),
        description=_clip(agent.summary, 4096),
        color=_color(proposal),
    )
    embed.add_field(
        name="Captain / Vice",
        value=_clip(
            f"(C) {agent.captaincy.captain_name or agent.captaincy.captain_id}\n"
            f"(V) {agent.captaincy.vice_captain_name or agent.captaincy.vice_captain_id}"
        ),
        inline=True,
    )

    extras = [f"Confidence: {agent.confidence:.0%}"]
    if agent.points_hit:
        extras.append(f"Hit: -{agent.points_hit}")
    if agent.chip:
        extras.append(f"Chip: {agent.chip}")
    embed.add_field(name="Plan", value=_clip("\n".join(extras)), inline=True)

    if agent.transfers:
        lines = []
        for move in agent.transfers:
            price = ""
            if move.selling_price and move.purchase_price:
                price = f" (sell £{move.selling_price / 10:.1f}m, buy £{move.purchase_price / 10:.1f}m)"
            lines.append(
                f"{move.player_out_name or move.element_out} -> "
                f"{move.player_in_name or move.element_in}{price}"
            )
        embed.add_field(name="Transfers", value=_clip("\n".join(lines)), inline=False)
    else:
        embed.add_field(name="Transfers", value="None (rolling)", inline=False)

    if agent.risks:
        embed.add_field(
            name="Risks", value=_clip("\n".join(f"• {r}" for r in agent.risks)), inline=False
        )

    if proposal.validation_issues:
        lines = [
            f"{'FATAL' if issue.fatal else 'warn'}: {issue.message}"
            for issue in proposal.validation_issues
        ]
        embed.add_field(name="Validation", value=_clip("\n".join(lines)), inline=False)

    embed.add_field(name="Status", value=_clip(_status_line(proposal, settings)), inline=False)

    written = proposal.created_at.astimezone().strftime("%a %d %b %H:%M %Z")
    footer = f"Deadline {deadline} · written {written} · {proposal.id}"
    if settings.dry_run:
        footer += " · DRY_RUN on"
    embed.set_footer(text=_clip(footer, 2048))
    embed.url = review_url(settings, proposal.id)
    return embed
