"""Signed approval links.

The whole point is one tap from a phone notification. That means the link itself
is the credential, so:

* it is signed with ``APPROVAL_SECRET`` and carries an expiry (``itsdangerous``
  timed serialiser), and rotating the secret invalidates every old link;
* it is scoped to a single proposal id, so a leaked link cannot approve next
  week's plan;
* **following the link never changes anything.** Mail clients, chat apps and
  link scanners fetch URLs on their own, and a GET that approves would mean a
  spam filter could pick your captain. The link opens a review page; the actions
  are POSTs from that page.
"""

from __future__ import annotations

import logging

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import Settings

logger = logging.getLogger(__name__)

SALT = "fpl-buddy-approval"


class InvalidApprovalToken(RuntimeError):
    pass


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        settings.approval_secret.get_secret_value(), salt=SALT
    )


def make_token(settings: Settings, proposal_id: str) -> str:
    return _serializer(settings).dumps({"id": proposal_id})


def read_token(settings: Settings, token: str) -> str:
    """Return the proposal id a token refers to, or raise."""
    max_age = settings.approval_link_ttl_hours * 3600
    try:
        payload = _serializer(settings).loads(token, max_age=max_age)
    except SignatureExpired as exc:
        raise InvalidApprovalToken(
            f"This link expired (links last {settings.approval_link_ttl_hours}h). "
            "Use the CLI or ask for a fresh notification."
        ) from exc
    except BadSignature as exc:
        raise InvalidApprovalToken("This link is not valid.") from exc

    proposal_id = (payload or {}).get("id")
    if not proposal_id:
        raise InvalidApprovalToken("This link does not name a proposal.")
    return str(proposal_id)


def review_url(settings: Settings, proposal_id: str) -> str:
    return f"{settings.public_base_url}/a/{make_token(settings, proposal_id)}"
