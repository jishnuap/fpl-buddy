"""FPL authentication.

FPL moved to OAuth: the session is a short-lived ``access_token`` plus a
long-lived ``refresh_token``, both issued by PingOne and carried as cookies. The
old ``pl_profile`` + ``sessionid`` pair no longer appears, and
``/api/my-team/{entry}/`` answers ``403 "Authentication credentials were not
provided."`` unless the access token is sent as a bearer header.

The lifetimes are what drive the design here:

* the **access token lasts 8 hours**, and this service proposes at T-36h and
  commits at T-45m. The token is therefore *guaranteed* to be dead by commit
  time, so refreshing is not an optimisation -- without it the deadline job
  cannot submit.
* the **refresh token lasts ~180 days** and carries ``offline_access``, so it can
  mint new access tokens without a browser.

Three ways to get a session, tried in order:

1. **Cached tokens** on disk, refreshed automatically when the access token is
   near expiry.
2. **Pasted cookie header** (``FPL_COOKIE_HEADER``) from DevTools -> Network ->
   any ``/api/me/`` request -> the ``cookie`` request header. This is the normal
   way in, and the only way from a datacenter IP, where Premier League's bot
   protection rejects programmatic login outright.
3. **Legacy login** against ``users.premierleague.com/accounts/login/``, kept for
   the pre-OAuth cookie scheme. Redirects must not be followed: the ``302``
   carries the ``Set-Cookie`` headers, and following the hop loses them.

.. warning::
   PingOne **rotates the refresh token** on every use, so the cache on disk
   becomes the only valid copy the moment a refresh happens. If ``STATE_DIR`` is
   not durable you get exactly one refresh per paste, and then the deadline job
   starts failing. Mount a volume.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings

logger = logging.getLogger(__name__)

# Pre-OAuth cookies. Kept so an old pasted header still works.
REQUIRED_COOKIES = ("pl_profile", "sessionid")

# Fallback lifetime for the legacy scheme, which carries no expiry we can read.
COOKIE_MAX_AGE_SECONDS = 60 * 60 * 12

# Observed OAuth client, used only when the token itself doesn't say. The values
# are derived from the token's own claims first, so an FPL-side change to the
# PingOne environment needs no code change.
DEFAULT_TOKEN_ISSUER = "https://auth.pingone.eu/68340de1-dfb9-412e-937c-20172986d129/as"
DEFAULT_CLIENT_ID = "1f243d70-a140-4035-8c41-341f5af5aa12"


class FPLAuthError(RuntimeError):
    """Raised when we cannot obtain a usable authenticated session."""


class FPLTokenRefreshError(FPLAuthError):
    """The refresh token was rejected, so only a fresh paste can recover."""


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT's payload **without verifying its signature**.

    That is deliberate and safe here: we are not making a trust decision, only
    reading ``exp`` to know when to refresh and ``iss``/``client_id`` to know
    where to refresh. The authority on whether a token is valid is the API that
    rejects it. Returns ``{}`` for anything that isn't a JWT, so the legacy
    cookie scheme flows through unharmed.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}


@dataclass
class SessionCookies:
    cookies: dict[str, str] = field(default_factory=dict)
    obtained_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ tokens
    @property
    def access_token(self) -> str:
        return self.cookies.get("access_token", "")

    @property
    def refresh_token(self) -> str:
        return self.cookies.get("refresh_token", "")

    @property
    def is_oauth(self) -> bool:
        return bool(self.access_token)

    @property
    def claims(self) -> dict[str, Any]:
        return decode_jwt_claims(self.access_token)

    @property
    def expires_at(self) -> float | None:
        """Epoch seconds the access token dies, or None if it doesn't say."""
        exp = self.claims.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None

    def expires_in(self, *, now: float | None = None) -> float | None:
        expiry = self.expires_at
        return None if expiry is None else expiry - (now or time.time())

    @property
    def token_issuer(self) -> str:
        return str(self.claims.get("iss") or DEFAULT_TOKEN_ISSUER)

    @property
    def token_url(self) -> str:
        return f"{self.token_issuer.rstrip('/')}/token"

    @property
    def client_id(self) -> str:
        return str(self.claims.get("client_id") or DEFAULT_CLIENT_ID)

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token)

    # ------------------------------------------------------------------ health
    @property
    def is_complete(self) -> bool:
        return self.is_oauth or all(self.cookies.get(name) for name in REQUIRED_COOKIES)

    def is_expiring(self, *, skew_seconds: float = 300.0) -> bool:
        """True when the access token is dead, or close enough to it to matter."""
        remaining = self.expires_in()
        return remaining is not None and remaining <= skew_seconds

    @property
    def is_stale(self) -> bool:
        """Whether these credentials should be replaced before being used.

        For OAuth the token's own ``exp`` is authoritative. Wall-clock age is
        only a fallback for the legacy scheme, where nothing tells us the
        lifetime -- guessing 12 hours there beats trusting a blob forever.
        """
        if self.expires_at is not None:
            return self.is_expiring()
        return (time.time() - self.obtained_at) > COOKIE_MAX_AGE_SECONDS

    # ------------------------------------------------------------------- shape
    def as_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def with_tokens(self, *, access_token: str, refresh_token: str = "") -> SessionCookies:
        """A copy carrying new tokens, keeping every other cookie.

        The rest of the jar matters: ``datadome`` and ``cf_clearance`` are what
        the bot protection looks at, so dropping them trades an auth failure for
        a different one.
        """
        cookies = dict(self.cookies)
        cookies["access_token"] = access_token
        if refresh_token:
            cookies["refresh_token"] = refresh_token
        return SessionCookies(cookies=cookies, obtained_at=time.time())

    def describe(self) -> str:
        """One line for the CLI and the logs. Never includes a token value."""
        if not self.is_oauth:
            age = (time.time() - self.obtained_at) / 3600
            return f"legacy cookie session, {age:.1f}h old, stale={self.is_stale}"
        remaining = self.expires_in()
        window = "unknown" if remaining is None else f"{remaining / 60:.0f} min"
        refresh_exp = decode_jwt_claims(self.refresh_token).get("exp")
        refresh_note = "absent"
        if self.refresh_token:
            refresh_note = "present"
            if isinstance(refresh_exp, (int, float)):
                days = (refresh_exp - time.time()) / 86400
                refresh_note = f"present, {days:.0f} days left"
        return (
            f"OAuth session, access token expires in {window}, "
            f"refresh token {refresh_note}"
        )

    def to_dict(self) -> dict:
        return {"cookies": self.cookies, "obtained_at": self.obtained_at}

    @classmethod
    def from_dict(cls, data: dict) -> SessionCookies:
        return cls(cookies=data.get("cookies", {}), obtained_at=data.get("obtained_at", 0.0))


def parse_cookie_header(header: str) -> dict[str, str]:
    """Turn a raw ``cookie:`` request header into a dict."""
    out: dict[str, str] = {}
    for chunk in header.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        out[name.strip()] = value.strip()
    return out


class CookieCache:
    """Tiny on-disk cache so restarts don't force a fresh login."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> SessionCookies | None:
        if not self.path.exists():
            return None
        try:
            return SessionCookies.from_dict(json.loads(self.path.read_text()))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read cookie cache at %s: %s", self.path, exc)
            return None

    def save(self, session: SessionCookies) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(session.to_dict()))
            self.path.chmod(0o600)
        except OSError as exc:
            logger.warning("Could not write cookie cache at %s: %s", self.path, exc)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class FPLAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cache = CookieCache(Path(settings.state_dir) / "fpl_cookies.json")

    # ------------------------------------------------------------------ public
    def get_session_cookies(self, *, force_refresh: bool = False) -> SessionCookies:
        """Return usable credentials, refreshing or logging in only if needed.

        Cheap to call on every request: the common path is a cache hit with a
        token that is still valid.
        """
        cached = self.cache.load()

        if cached and cached.is_complete and not force_refresh and not cached.is_stale:
            logger.debug("Using cached FPL session (%s).", cached.describe())
            return cached

        # An expiring OAuth session is a refresh, not a re-paste. This is the
        # path that keeps the deadline job alive 36 hours after proposing.
        refresh_error: FPLTokenRefreshError | None = None
        if cached and cached.can_refresh:
            try:
                return self._refresh(cached)
            except FPLTokenRefreshError as exc:
                # Not fatal yet: the env var may hold a newer session than the
                # cache. Keep the error, though -- if nothing else works it is
                # the honest explanation, and it says how to recover.
                refresh_error = exc
                logger.warning("Refresh with the cached token failed (%s).", exc)

        # Explicit cookie header next -- the operator saying "use these".
        if self.settings.has_cookie_header:
            raw = self.settings.fpl_cookie_header.get_secret_value()
            session = SessionCookies(cookies=parse_cookie_header(raw))
            if not session.is_complete:
                raise FPLAuthError(
                    "FPL_COOKIE_HEADER is missing required authentication tokens "
                    "(must contain access_token or pl_profile + sessionid)"
                )
            # A pasted header can itself be hours old by the time it is used.
            if session.is_oauth and session.is_expiring() and session.can_refresh:
                logger.info("Pasted access token is expiring; refreshing it now.")
                return self._refresh(session)
            self.cache.save(session)
            return session

        if not self.settings.has_login_credentials:
            # Report the real cause. "No credentials" would send someone to check
            # their environment when the actual problem is a spent refresh token.
            if refresh_error is not None:
                raise refresh_error
            raise FPLAuthError(
                "No FPL credentials. Paste FPL_COOKIE_HEADER from your browser "
                "(DevTools -> Network -> any /api/me/ request -> cookie), or set "
                "FPL_EMAIL + FPL_PASSWORD for the legacy login."
            )

        session = self._login()
        self.cache.save(session)
        return session

    def invalidate(self) -> None:
        self.cache.clear()

    # ----------------------------------------------------------------- refresh
    def _refresh(self, session: SessionCookies) -> SessionCookies:
        """Exchange the refresh token for a new access token.

        FPL's OAuth client is public, so this needs ``client_id`` and no secret.
        PingOne rotates the refresh token on use, which makes persisting the
        response mandatory: miss it and the next refresh presents a token that
        has already been spent.
        """
        if not session.can_refresh:
            raise FPLTokenRefreshError(
                "No refresh token available, so the access token cannot be renewed. "
                "Re-paste FPL_COOKIE_HEADER from your browser."
            )

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": session.refresh_token,
            "client_id": session.client_id,
        }
        try:
            with httpx.Client(
                timeout=self.settings.http_timeout_seconds, follow_redirects=False
            ) as client:
                response = client.post(
                    session.token_url,
                    data=payload,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                        "User-Agent": self.settings.user_agent,
                    },
                )
        except httpx.HTTPError as exc:
            raise FPLTokenRefreshError(f"Token refresh request failed: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:300]
            raise FPLTokenRefreshError(
                f"Token refresh was rejected with {response.status_code}: {detail}. "
                "Refresh tokens are single-use and expire after about 180 days -- "
                "re-paste FPL_COOKIE_HEADER from your browser."
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise FPLTokenRefreshError("Token endpoint returned a non-JSON body.") from exc

        access_token = body.get("access_token")
        if not access_token:
            raise FPLTokenRefreshError(
                f"Token endpoint returned no access_token (keys: {sorted(body)})."
            )

        refreshed = session.with_tokens(
            access_token=access_token,
            # Absent means "keep using the old one"; present means it rotated and
            # the previous value is already dead.
            refresh_token=body.get("refresh_token", ""),
            )
        self.cache.save(refreshed)
        logger.info("Refreshed the FPL access token (%s).", refreshed.describe())
        return refreshed

    def refresh_now(self) -> SessionCookies:
        """Force a refresh. Used by the CLI to prove the flow works."""
        session = self.cache.load()
        if session is None or not session.can_refresh:
            if not self.settings.has_cookie_header:
                raise FPLTokenRefreshError(
                    "Nothing to refresh. Paste FPL_COOKIE_HEADER from your browser."
                )
            session = SessionCookies(
                cookies=parse_cookie_header(self.settings.fpl_cookie_header.get_secret_value())
            )
        return self._refresh(session)

    def peek(self) -> SessionCookies | None:
        """Whatever credentials we currently hold, without touching the network."""
        cached = self.cache.load()
        if cached is not None:
            return cached
        if self.settings.has_cookie_header:
            return SessionCookies(
                cookies=parse_cookie_header(self.settings.fpl_cookie_header.get_secret_value())
            )
        return None

    # ----------------------------------------------------------------- private
    def _login(self) -> SessionCookies:
        payload = {
            "login": self.settings.fpl_email,
            "password": self.settings.fpl_password.get_secret_value(),
            "app": "plfpl-web",
            "redirect_uri": "https://fantasy.premierleague.com/a/login",
        }
        headers = {
            "User-Agent": self.settings.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://fantasy.premierleague.com",
            "Referer": "https://fantasy.premierleague.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        }

        # follow_redirects=False is the whole trick. Do not change it.
        with httpx.Client(
            follow_redirects=False,
            timeout=self.settings.http_timeout_seconds,
        ) as client:
            try:
                response = client.post(
                    self.settings.fpl_login_url, data=payload, headers=headers
                )
            except httpx.HTTPError as exc:
                raise FPLAuthError(f"Login request failed: {exc}") from exc

            cookies = {name: value for name, value in client.cookies.items()}

        # A successful login is a 302 towards the redirect_uri. A 200 usually
        # means the login form was re-rendered with an error, and a 403 means
        # the bot protection ate the request.
        if response.status_code == 403:
            raise FPLAuthError(
                "Login returned 403 -- Premier League's bot protection rejected this IP. "
                "This is common from cloud/datacenter IPs. Use FPL_COOKIE_HEADER instead."
            )

        missing = [name for name in REQUIRED_COOKIES if name not in cookies]
        if missing:
            location = response.headers.get("location", "")
            hint = ""
            if "state=fail" in location or "error" in location.lower():
                hint = " The redirect suggests bad credentials."
            raise FPLAuthError(
                f"Login did not yield {', '.join(missing)} (HTTP {response.status_code}).{hint} "
                "If credentials are correct, fall back to FPL_COOKIE_HEADER."
            )

        logger.info("Logged in to FPL, obtained %d cookies.", len(cookies))
        return SessionCookies(cookies=cookies)
