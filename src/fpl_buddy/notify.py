"""Getting the proposal in front of the human.

Pluggable because the right channel is personal: ``log`` while you're building,
``smtp`` for something that survives on your phone, ``webhook`` to fan out into
Telegram/Slack/ntfy/Shortcuts.

A failed notification must never break the run. If the message doesn't go out,
the proposal still exists and still auto-commits -- so notification errors are
logged loudly and swallowed.
"""

from __future__ import annotations

import json
import logging
import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Any

import httpx

from .approval import review_url
from .config import Settings
from .decisions.schema import Proposal, ProposalStatus

logger = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, subject: str, text: str, *, html: str | None = None, meta: dict | None = None) -> None: ...

    def notify_proposal(self, proposal: Proposal, settings: Settings) -> None:
        subject, text, html = render_proposal(proposal, settings)
        self.send(subject, text, html=html, meta={"proposal_id": proposal.id})


class NullNotifier(Notifier):
    def send(self, subject, text, *, html=None, meta=None) -> None:
        logger.debug("Notifications disabled; dropping %r", subject)


class LogNotifier(Notifier):
    def send(self, subject, text, *, html=None, meta=None) -> None:
        logger.info("NOTIFY: %s\n%s", subject, text)


class SmtpNotifier(Notifier):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, subject, text, *, html=None, meta=None) -> None:
        s = self.settings
        if not (s.smtp_host and s.smtp_from and s.smtp_to):
            raise RuntimeError("SMTP notification needs SMTP_HOST, SMTP_FROM and SMTP_TO.")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = s.smtp_from
        message["To"] = s.smtp_to
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")

        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.http_timeout_seconds) as smtp:
            smtp.ehlo()
            if s.smtp_port != 25:
                smtp.starttls()
                smtp.ehlo()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)
        logger.info("Emailed %r to %s.", subject, s.smtp_to)


class WebhookNotifier(Notifier):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, subject, text, *, html=None, meta=None) -> None:
        if not self.settings.webhook_url:
            raise RuntimeError("WEBHOOK_URL is not set.")
        payload = {"subject": subject, "text": text, **(meta or {})}
        with httpx.Client(timeout=self.settings.http_timeout_seconds) as client:
            response = client.post(self.settings.webhook_url, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Webhook returned {response.status_code}: {response.text[:200]}"
            )
        logger.info("Posted %r to the webhook.", subject)


def build_notifier(settings: Settings, *, discord_bot: Any = None) -> Notifier:
    match settings.notify_channel:
        case "none":
            return NullNotifier()
        case "smtp":
            return SmtpNotifier(settings)
        case "webhook":
            return WebhookNotifier(settings)
        case "discord":
            # With a gateway bot: an embed plus working Approve/Amend/Reject
            # buttons. Without one -- the tick job, or a web service running
            # with SCHEDULER_ENABLED=false -- the same embed goes over plain
            # HTTPS and the approval link carries the interaction instead.
            # Nothing here should refuse to notify just because it has no
            # WebSocket; the proposal exists either way and the human needs to
            # hear about it.
            if discord_bot is not None:
                from .discord_bot.notifier import DiscordNotifier

                return DiscordNotifier(discord_bot, settings)

            from .discord_bot.rest import DiscordRestNotifier

            return DiscordRestNotifier(settings)
        case _:
            return LogNotifier()


def safe_notify(notifier: Notifier, proposal: Proposal, settings: Settings) -> None:
    """Notify, but never let a delivery failure take the run down with it."""
    try:
        notifier.notify_proposal(proposal, settings)
    except Exception as exc:  # noqa: BLE001 - deliberate: notification is best-effort
        logger.error(
            "Could not notify about proposal %s (%s). The proposal is stored and will "
            "still auto-commit.", proposal.id, exc,
        )


def render_errors(errors: list[str]) -> tuple[str, str]:
    """``(subject, text)`` for one or more failures from the same scheduled run."""
    subject = "FPL tick failed" if len(errors) == 1 else f"FPL tick failed ({len(errors)} errors)"
    text = "\n".join(f"- {e}" for e in errors)
    return subject, text


def safe_notify_errors(notifier: Notifier, errors: list[str], settings: Settings) -> None:
    """Tell Discord a scheduled run failed. Best-effort, like every other notify here.

    The caller (tick.py / scheduler.py) already decided this is worth saying --
    a new failure, or a repeat outside the cooldown -- so this only has to
    render and send without itself becoming another way for the run to fail.
    """
    if not settings.notify_errors or not errors:
        return
    try:
        subject, text = render_errors(errors)
        notifier.send(subject, text, meta={"kind": "error"})
    except Exception as exc:  # noqa: BLE001 - deliberate: notification is best-effort
        logger.error("Could not send the tick-failure notification (%s). Errors: %s", exc, errors)


def safe_notify_harvest(notifier: Notifier, report, settings: Settings) -> None:
    """Send the harvest summary. Doubly best-effort.

    The harvest is already optional enrichment, so a failed message about a
    failed-tolerant job must not be the thing that raises. Swallowed at WARNING
    rather than ERROR for that reason: nothing downstream depends on it.
    """
    if not settings.notify_harvest:
        return
    try:
        subject, text = render_harvest(report, settings)
        notifier.send(subject, text, meta={"kind": "harvest"})
    except Exception as exc:  # noqa: BLE001 - deliberate: notification is best-effort
        logger.warning("Could not send the harvest summary (%s). The articles are stored.", exc)


# --------------------------------------------------------------------- rendering


def render_proposal(proposal: Proposal, settings: Settings) -> tuple[str, str, str]:
    """``(subject, plain text, html)`` for one proposal."""
    agent = proposal.agent
    url = review_url(settings, proposal.id)
    deadline = proposal.deadline.astimezone().strftime("%a %d %b %H:%M %Z")

    subject = f"FPL GW{proposal.gameweek}: {proposal.headline()}"

    lines = [
        f"Gameweek {proposal.gameweek} -- deadline {deadline}",
        "",
        f"Captain:      {agent.captaincy.captain_name or agent.captaincy.captain_id}",
        f"Vice:         {agent.captaincy.vice_captain_name or agent.captaincy.vice_captain_id}",
    ]
    if agent.transfers:
        for move in agent.transfers:
            price = ""
            if move.selling_price and move.purchase_price:
                price = (
                    f"  (sell £{move.selling_price / 10:.1f}m, "
                    f"buy £{move.purchase_price / 10:.1f}m)"
                )
            lines.append(
                f"Transfer:     {move.player_out_name or move.element_out} -> "
                f"{move.player_in_name or move.element_in}{price}"
            )
    else:
        lines.append("Transfers:    none (rolling)")
    if agent.points_hit:
        lines.append(f"Hit:          -{agent.points_hit}")
    if agent.chip:
        lines.append(f"Chip:         {agent.chip}")
    lines += [
        f"Confidence:   {agent.confidence:.0%}",
        "",
        "Why:",
        f"  {agent.summary}",
        "",
        f"Armband:      {agent.captaincy.reason}",
    ]
    for move in agent.transfers:
        if move.reason:
            lines.append(f"Transfer:     {move.reason}")
    if agent.risks:
        lines += ["", "Risks:", *(f"  - {r}" for r in agent.risks)]

    if proposal.validation_issues:
        lines += ["", "Validation:"]
        for issue in proposal.validation_issues:
            lines.append(f"  [{'FATAL' if issue.fatal else 'warn'}] {issue.message}")

    if proposal.fatal_issues:
        lines += [
            "",
            "!! This proposal will NOT be submitted -- it failed validation. "
            "Amend it or act in the app.",
        ]
    elif proposal.status in (ProposalStatus.EXECUTED, ProposalStatus.AUTO_EXECUTED):
        lines += [
            "",
            "Submitted to FPL."
            + (" (DRY_RUN was on, so nothing really went out.)" if settings.dry_run else ""),
        ]
    elif proposal.status == ProposalStatus.FAILED:
        lines += [
            "",
            "!! Submission failed. Nothing will be retried automatically -- check the "
            "app and act by hand.",
        ]
    elif proposal.is_terminal:
        lines += ["", f"Status: {proposal.status.value}. Nothing further will happen."]
    elif settings.auto_commit_enabled:
        lines += [
            "",
            f"Doing nothing submits this automatically about "
            f"{settings.commit_minutes_before_deadline:.0f} minutes before the deadline.",
        ]
    else:
        lines += ["", "Auto-commit is off: without approval, nothing is submitted."]

    lines += ["", f"Review / approve / reject: {url}"]
    if settings.dry_run:
        lines += ["", "(DRY_RUN is on -- nothing will actually be sent to FPL.)"]

    text = "\n".join(lines)
    return subject, text, _html(proposal, text, url)


def _html(proposal: Proposal, text: str, url: str) -> str:
    from jinja2 import Template

    template = Template(
        """
<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
                   max-width:34rem;margin:0 auto;padding:1.5rem;color:#111">
  <h2 style="margin:0 0 .25rem">GW{{ p.gameweek }}</h2>
  <p style="margin:0 0 1rem;color:#555">{{ p.headline() }}</p>
  {% if p.fatal_issues %}
  <p style="background:#fdecea;border-left:4px solid #d32f2f;padding:.75rem;margin:0 0 1rem">
    Failed validation -- this will not be submitted.
  </p>
  {% endif %}
  <pre style="white-space:pre-wrap;background:#f6f6f6;padding:1rem;border-radius:.5rem;
              font-size:.85rem;line-height:1.45">{{ body }}</pre>
  <p><a href="{{ url }}"
        style="display:inline-block;background:#111;color:#fff;text-decoration:none;
               padding:.7rem 1.2rem;border-radius:.4rem">Review this proposal</a></p>
</body></html>
"""
    )
    return template.render(p=proposal, body=text, url=url)


def render_json(proposal: Proposal) -> str:
    """For the webhook channel and for eyeballing state on the CLI."""
    return json.dumps(proposal.model_dump(mode="json"), indent=2)


# Discord hard-rejects a message over 2000 characters, and a harvest that picks
# up thirty videos would sail past that. The cap is on the article list, since
# that is the only unbounded part.
HARVEST_ARTICLE_LIMIT = 12
_KEY_POINT_CHARS = 160


def render_harvest(report, settings: Settings) -> tuple[str, str]:
    """``(subject, plain text)`` for one harvest run.

    Leads with what was found rather than with the counters. A summary that says
    "6 new article(s) from 41 candidate(s)" tells you the machinery ran; it does
    not tell you whether it found the injury news you care about.
    """
    stored = list(getattr(report, "notes", []) or [])
    subject = (
        f"FPL harvest: {report.stored} new "
        + ("article" if report.stored == 1 else "articles")
        + (f", {len(report.failures)} failure(s)" if report.failures else "")
    )

    if not stored:
        lines = ["Nothing new this time."]
    else:
        lines = []
        for note in stored[:HARVEST_ARTICLE_LIMIT]:
            kind = "video" if note.kind == "youtube" else "article"
            gated = " [partial]" if note.access != "full" else ""
            lines.append(f"* {note.title}")
            lines.append(f"  {note.source} - {kind}{gated}")
            # One line of substance per item. The whole point is to be readable
            # on a phone without opening anything.
            point = (note.key_points or [note.summary or ""])[0].strip()
            if point:
                if len(point) > _KEY_POINT_CHARS:
                    point = point[: _KEY_POINT_CHARS - 1].rstrip() + "…"
                lines.append(f"  {point}")
            lines.append(f"  {note.url}")
        hidden = len(stored) - HARVEST_ARTICLE_LIMIT
        if hidden > 0:
            lines.append(f"...and {hidden} more.")

    lines += ["", report.summary()]

    if report.failures:
        # Truncated for the same length reason, but never hidden entirely: a
        # source that has quietly broken looks identical to a quiet news day.
        lines += ["", "Failures:", *(f"  - {f}" for f in report.failures[:5])]
        if len(report.failures) > 5:
            lines.append(f"  ...and {len(report.failures) - 5} more.")

    return subject, "\n".join(lines)
