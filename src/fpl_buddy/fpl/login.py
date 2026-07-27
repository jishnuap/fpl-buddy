"""Headless email + password login against Premier League's OAuth provider.

FPL's login lives on PingOne DaVinci at ``account.premierleague.com``. There is
no documented API for it, but the browser flow is a plain OAuth 2.0
authorization-code exchange with PKCE and a public client, so it can be driven
end to end without a browser. That is what this module does, and it is what
makes the service self-healing: any process holding ``FPL_EMAIL`` and
``FPL_PASSWORD`` can mint a brand new session, rather than depending on a
refresh token that dies the moment a single link in the chain is lost.

The flow, five requests:

1. ``GET /as/authorize`` with a PKCE challenge. The HTML that comes back carries
   a DaVinci ``accessToken`` and the ``state`` needed to resume later.
2. ``POST /davinci/policy/{policy}/start`` with that token, for an
   ``interactionId``.
3. Three posts to ``/davinci/connections/{conn}/capabilities/customHTMLTemplate``
   -- poll, submit the credentials, confirm -- ending in a ``dvResponse``.
4. ``POST /as/resume`` with the ``dvResponse``. Redirects must not be followed:
   the authorization ``code`` is in the ``Location`` header, and following the
   hop loses it.
5. ``POST /as/token`` exchanging the code plus the PKCE verifier for tokens.

Two details are doing the heavy lifting:

* **TLS impersonation.** Premier League's bot protection fingerprints the TLS
  handshake, and a stock Python client fails it no matter what headers it sends.
  ``curl_cffi`` presents Chrome's fingerprint, which is the whole reason this
  works where a plain ``httpx`` port of the same requests does not.
* **``offline_access`` in the scope.** The token response therefore includes a
  refresh token, so the cheap refresh path keeps working and a full login is
  only needed when refresh is genuinely unavailable.

Credit: the flow was worked out by the AIrsenal project
(``alan-turing-institute/AIrsenal``, ``airsenal/framework/data_fetcher.py``).

.. warning::
   This is a private flow, not an API. The policy and connection ids below, and
   the regexes that read the login page, are all things Premier League can
   change without notice. Every failure here names the step it failed at so the
   logs say which half of the flow moved.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import uuid
from typing import Any

from ..config import Settings
from .auth import FPLAuthError, SessionCookies

logger = logging.getLogger(__name__)

LOGIN_BASE = "https://account.premierleague.com"
LOGIN_URLS = {
    "auth": f"{LOGIN_BASE}/as/authorize",
    "start": f"{LOGIN_BASE}/davinci/policy/262ce4b01d19dd9d385d26bddb4297b6/start",
    "login": f"{LOGIN_BASE}/davinci/connections/{{}}/capabilities/customHTMLTemplate",
    "resume": f"{LOGIN_BASE}/as/resume",
    "token": f"{LOGIN_BASE}/as/token",
}

# The Premier League web client. Public, so the exchange needs no secret -- PKCE
# is what proves the code was redeemed by whoever requested it.
CLIENT_ID = "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"
STANDARD_CONNECTION_ID = "867ed4363b2bc21c860085ad2baa817d"
REDIRECT_URI = "https://fantasy.premierleague.com/"
SCOPE = "openid profile email offline_access"

# Cookies worth carrying from the login session into the API session. The rest
# of the jar is PingOne's own session state, scoped to a host we never call
# again; a browser would not send it to fantasy.premierleague.com either.
CARRIED_COOKIES = ("datadome", "cf_clearance")


class FPLLoginError(FPLAuthError):
    """A step of the password login flow failed.

    ``step`` is kept as an attribute as well as being in the message, so callers
    can tell "Premier League rejected the password" from "the login page moved"
    without parsing prose.
    """

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"FPL login failed at {step}: {message}")
        self.step = step


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def build_session(settings: Settings) -> Any:
    """A ``curl_cffi`` session impersonating Chrome.

    Imported lazily, and reported clearly if it is missing: an ImportError at
    module import time would take down every command in the CLI, including the
    ones that have nothing to do with logging in.
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:  # pragma: no cover - packaging failure, not logic
        raise FPLLoginError(
            "setup",
            "curl_cffi is not installed, so the login cannot present a browser "
            "TLS fingerprint (pip install curl_cffi). Set FPL_COOKIE_HEADER to "
            "get in without it.",
        ) from exc
    # Typed as a Literal of known browser builds upstream; ours comes from the
    # environment on purpose, so a new Chrome build is a config change.
    impersonate: Any = settings.fpl_login_impersonate
    try:
        return curl_requests.Session(impersonate=impersonate)
    except (ValueError, RuntimeError) as exc:
        raise FPLLoginError(
            "setup", f"curl_cffi rejected FPL_LOGIN_IMPERSONATE={impersonate!r}: {exc}"
        ) from exc


def password_login(settings: Settings, *, session: Any | None = None) -> SessionCookies:
    """Log in with ``FPL_EMAIL`` / ``FPL_PASSWORD`` and return a live session.

    ``session`` exists so tests can drive the whole flow against canned
    responses; production always builds its own impersonating session.
    """
    if not settings.has_login_credentials:
        raise FPLLoginError("setup", "FPL_EMAIL and FPL_PASSWORD are not both set.")

    rsession = session if session is not None else build_session(settings)
    code_verifier = generate_code_verifier()

    access_token, state = _authorize(rsession, code_verifier)
    interaction_id, response_id = _start_interaction(rsession, access_token)
    dv_response = _sign_on(settings, rsession, interaction_id, response_id, access_token)
    auth_code = _resume(rsession, dv_response, state)
    tokens = _exchange_code(rsession, auth_code, code_verifier)

    cookies = {
        name: value
        for name, value in _jar(rsession).items()
        if name in CARRIED_COOKIES
    }
    cookies["access_token"] = tokens["access_token"]
    if tokens.get("refresh_token"):
        cookies["refresh_token"] = tokens["refresh_token"]

    result = SessionCookies(cookies=cookies)
    logger.info("Logged in to FPL with a password (%s).", result.describe())
    return result


# ------------------------------------------------------------------- the steps


def _authorize(rsession: Any, code_verifier: str) -> tuple[str, str]:
    """Step 1: the login page, which carries a DaVinci token and a state."""
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "state": uuid.uuid4().hex,
        "code_challenge": generate_code_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    response = rsession.get(LOGIN_URLS["auth"], params=params)
    _check_status(response, "authorize")
    html = response.text or ""

    token = _extract(r'"accessToken":"([^"]+)"', html)
    if token is None:
        raise FPLLoginError(
            "authorize",
            "no accessToken in the login page. Premier League may have changed "
            "the page, or the bot protection served a challenge instead.",
        )
    # Read the state here rather than reusing ours: the resume step must echo
    # back the value the provider put in the form.
    state = _extract(r'<input[^>]+name="state"[^>]+value="([^"]+)"', html)
    if state is None:
        raise FPLLoginError("authorize", "no state field in the login page.")
    return token, state


def _start_interaction(rsession: Any, access_token: str) -> tuple[str, str]:
    """Step 2: open a DaVinci interaction to log in inside."""
    response = rsession.post(LOGIN_URLS["start"], headers=_bearer(access_token))
    _check_status(response, "start")
    body = _json(response, "start")
    try:
        return str(body["interactionId"]), str(body["id"])
    except KeyError as exc:
        raise FPLLoginError("start", f"no {exc} in the interaction response.") from exc


def _sign_on(
    settings: Settings,
    rsession: Any,
    interaction_id: str,
    response_id: str,
    access_token: str,
) -> str:
    """Step 3: poll, submit the credentials, confirm. Returns the dvResponse."""
    headers = {"interactionId": interaction_id}

    response = rsession.post(
        LOGIN_URLS["login"].format(STANDARD_CONNECTION_ID),
        headers=headers,
        json={
            "id": response_id,
            "eventName": "continue",
            "parameters": {"eventType": "polling"},
            "pollProps": {
                "status": "continue",
                "delayInMs": 10,
                "retriesAllowed": 1,
                "pollChallengeStatus": False,
            },
        },
    )
    _check_status(response, "sign-on poll")
    response_id = str(_json(response, "sign-on poll").get("id") or "")
    if not response_id:
        raise FPLLoginError("sign-on poll", "no id in the poll response.")

    response = rsession.post(
        LOGIN_URLS["login"].format(STANDARD_CONNECTION_ID),
        headers=headers,
        json={
            "id": response_id,
            "nextEvent": _NEXT_EVENT,
            "parameters": {
                "buttonType": "form-submit",
                "buttonValue": "SIGNON",
                "username": settings.fpl_email,
                "password": settings.fpl_password.get_secret_value(),
            },
            "eventName": "continue",
        },
    )
    _check_status(response, "sign-on submit")
    body = _json(response, "sign-on submit")
    try:
        response_id, connection_id = str(body["id"]), str(body["connectionId"])
    except KeyError as exc:
        # This is where a wrong password lands, so say so -- the flow itself is
        # fine, the credentials are not.
        raise FPLLoginError(
            "sign-on submit",
            f"no {exc} in the response, which usually means FPL rejected the "
            "email or password.",
        ) from exc

    response = rsession.post(
        LOGIN_URLS["login"].format(connection_id),
        headers=_bearer(access_token),
        json={
            "id": response_id,
            "nextEvent": _NEXT_EVENT,
            "parameters": {"buttonType": "form-submit", "buttonValue": "SIGNON"},
            "eventName": "continue",
        },
    )
    _check_status(response, "sign-on confirm")
    dv_response = _json(response, "sign-on confirm").get("dvResponse")
    if not dv_response:
        raise FPLLoginError("sign-on confirm", "no dvResponse in the response.")
    return str(dv_response)


def _resume(rsession: Any, dv_response: str, state: str) -> str:
    """Step 4: hand the signed interaction back to OAuth for an auth code."""
    response = rsession.post(
        LOGIN_URLS["resume"],
        data={"dvResponse": dv_response, "state": state},
        allow_redirects=False,
    )
    location = response.headers.get("Location") or response.headers.get("location") or ""
    code = _extract(r"[?&]code=([^&]+)", location)
    if code is None:
        raise FPLLoginError(
            "resume",
            f"no authorization code in the redirect (HTTP {response.status_code}).",
        )
    return code


def _exchange_code(rsession: Any, auth_code: str, code_verifier: str) -> dict[str, Any]:
    """Step 5: the actual token exchange. PKCE, public client, no secret."""
    response = rsession.post(
        LOGIN_URLS["token"],
        data={
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": auth_code,
            "code_verifier": code_verifier,
            "client_id": CLIENT_ID,
        },
    )
    _check_status(response, "token exchange")
    body = _json(response, "token exchange")
    if not body.get("access_token"):
        raise FPLLoginError("token exchange", f"no access_token (keys: {sorted(body)}).")
    if not body.get("refresh_token"):
        # Not fatal: a login-per-run still works. It does mean every request
        # window pays for a full login, so it is worth knowing about.
        logger.warning("Token response carried no refresh_token despite offline_access.")
    return body


# ------------------------------------------------------------------- plumbing

# DaVinci wants the follow-up event described on every submit. It is the same
# object each time, so it lives here rather than being retyped.
_NEXT_EVENT = {
    "constructType": "skEvent",
    "eventName": "continue",
    "params": [],
    "eventType": "post",
    "postProcess": {},
}


def _bearer(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}


def _extract(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _check_status(response: Any, step: str) -> None:
    status = getattr(response, "status_code", 0)
    if status < 400:
        return
    hint = ""
    if status in (403, 429):
        hint = (
            " Premier League's bot protection rejected this request, which is "
            "what happens from most datacenter IPs. Set FPL_COOKIE_HEADER to "
            "get in from here."
        )
    raise FPLLoginError(step, f"HTTP {status}.{hint}")


def _json(response: Any, step: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise FPLLoginError(step, "the response was not JSON.") from exc
    if not isinstance(body, dict):
        raise FPLLoginError(step, f"the response was {type(body).__name__}, not an object.")
    return body


def _jar(rsession: Any) -> dict[str, str]:
    """Whatever cookies the login collected, as a plain dict.

    Defensive because this crosses a library boundary: a cookie jar that will
    not iterate is not a reason to throw away a session we just successfully
    logged in to.
    """
    try:
        return {str(name): str(value) for name, value in dict(rsession.cookies).items()}
    except Exception as exc:  # noqa: BLE001 - the tokens matter, the jar does not
        logger.debug("Could not read the login cookie jar: %s", exc)
        return {}
