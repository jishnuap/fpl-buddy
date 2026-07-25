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
            if discord_bot is None:
                raise RuntimeError(
                    "NOTIFY_CHANNEL=discord but no bot was passed to build_notifier()."
                )
            from .discord_bot.notifier import DiscordNotifier

            return DiscordNotifier(discord_bot, settings)
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
