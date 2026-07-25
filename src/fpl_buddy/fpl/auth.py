"""FPL authentication.

Two paths, tried in order:

1. **Programmatic login** against ``users.premierleague.com/accounts/login/``.
   The critical detail is that redirects must NOT be followed automatically.
   The login responds ``302`` and sets ``pl_profile`` + ``sessionid`` on that
   redirect response; if the client follows the hop, the ``Set-Cookie`` headers
   are consumed by the redirect chain and you end up with an anonymous session.
   ``httpx`` does not follow redirects by default, but we set it explicitly so
   nobody "helpfully" flips it later.

2. **Pasted cookie header** (``FPL_COOKIE_HEADER``). Premier League fronts the
   login with bot protection that regularly rejects datacenter IPs -- which is
   exactly what an Azure Container App is. When login fails, this is the escape
   hatch: open fantasy.premierleague.com in a browser, DevTools -> Network ->
   any ``/api/me/`` request -> copy the ``cookie`` request header.

Cookies are cached on disk so a warm container reuses a session instead of
hammering the login endpoint (which is a fast way to get rate-limited).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import Settings

logger = logging.getLogger(__name__)

# Cookies that actually matter for authorised requests.
REQUIRED_COOKIES = ("pl_profile", "sessionid")

# FPL sessions last a while, but re-validate rather than trust a stale blob.
COOKIE_MAX_AGE_SECONDS = 60 * 60 * 12


class FPLAuthError(RuntimeError):
    """Raised when we cannot obtain a usable authenticated session."""


@dataclass
class SessionCookies:
    cookies: dict[str, str] = field(default_factory=dict)
    obtained_at: float = field(default_factory=time.time)

    @property
    def is_complete(self) -> bool:
        return all(self.cookies.get(name) for name in REQUIRED_COOKIES)

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.obtained_at) > COOKIE_MAX_AGE_SECONDS

    def as_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

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
        """Return usable cookies, logging in only when necessary."""
        if not force_refresh:
            cached = self.cache.load()
            if cached and cached.is_complete and not cached.is_stale:
                logger.debug("Using cached FPL session cookies.")
                return cached

        # Explicit cookie header wins when present -- it's the operator saying
        # "login is blocked here, use these".
        if self.settings.has_cookie_header:
            raw = self.settings.fpl_cookie_header.get_secret_value()
            session = SessionCookies(cookies=parse_cookie_header(raw))
            if not session.is_complete:
                missing = [c for c in REQUIRED_COOKIES if c not in session.cookies]
                raise FPLAuthError(
                    f"FPL_COOKIE_HEADER is missing required cookie(s): {', '.join(missing)}"
                )
            self.cache.save(session)
            return session

        if not self.settings.has_login_credentials:
            raise FPLAuthError(
                "No FPL credentials. Set FPL_EMAIL + FPL_PASSWORD, or paste "
                "FPL_COOKIE_HEADER from your browser."
            )

        session = self._login()
        self.cache.save(session)
        return session

    def invalidate(self) -> None:
        self.cache.clear()

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
