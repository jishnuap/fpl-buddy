"""HTTP client for the Fantasy Premier League API (public reads + authorised writes).

Note on CORS: none of the write endpoints are callable from a browser -- FPL's
CORS policy blocks it. That is fine here; this always runs server-side.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import Settings
from .auth import FPLAuthenticator, FPLAuthError, SessionCookies
from .models import Bootstrap, Fixture, Gameweek, MyTeam, Pick, Player, Team

logger = logging.getLogger(__name__)


class FPLApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class TransferRejected(FPLApiError):
    """FPL refused the transfer -- price moved, player unavailable, budget, etc."""


class FPLClient:
    def __init__(self, settings: Settings, authenticator: FPLAuthenticator | None = None) -> None:
        self.settings = settings
        self.auth = authenticator or FPLAuthenticator(settings)
        self._session: SessionCookies | None = None
        self._bootstrap: Bootstrap | None = None

    # --------------------------------------------------------------- plumbing
    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.user_agent,
            "Accept": "application/json",
            "Accept-Language": "en-GB,en;q=0.9",
        }

    def _auth_headers(self, *, referer: str) -> dict[str, str]:
        if self._session is None:
            self._session = self.auth.get_session_cookies()
        headers = self._base_headers()
        headers.update(
            {
                "Cookie": self._session.as_header(),
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://fantasy.premierleague.com",
                "Referer": referer,
            }
        )
        if "access_token" in self._session.cookies:
            headers["Authorization"] = f"Bearer {self._session.cookies['access_token']}"
        return headers

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    def _get_json(self, url: str, *, authorised: bool = False) -> Any:
        headers = (
            self._auth_headers(referer="https://fantasy.premierleague.com/my-team")
            if authorised
            else self._base_headers()
        )
        with httpx.Client(timeout=self.settings.http_timeout_seconds) as client:
            response = client.get(url, headers=headers)

        if response.status_code in (401, 403) and authorised:
            # Session probably expired -- drop the cache and try once more.
            logger.info("Authorised GET returned %s; refreshing session.", response.status_code)
            self.auth.invalidate()
            self._session = self.auth.get_session_cookies(force_refresh=True)
            with httpx.Client(timeout=self.settings.http_timeout_seconds) as client:
                response = client.get(
                    url,
                    headers=self._auth_headers(referer="https://fantasy.premierleague.com/my-team"),
                )

        # 3xx included deliberately: the API answers reads with 200, so a redirect
        # means we've been bounced to a login page, not that there's data here.
        if response.status_code >= 300:
            raise FPLApiError(
                f"GET {url} failed with {response.status_code}",
                status_code=response.status_code,
                body=response.text[:1000],
            )
        return response.json()

    def _post_json(self, url: str, payload: dict, *, referer: str) -> dict:
        with httpx.Client(
            timeout=self.settings.http_timeout_seconds, follow_redirects=False
        ) as client:
            response = client.post(url, json=payload, headers=self._auth_headers(referer=referer))

        if response.status_code in (401, 403):
            self.auth.invalidate()
            self._session = self.auth.get_session_cookies(force_refresh=True)
            with httpx.Client(
                timeout=self.settings.http_timeout_seconds, follow_redirects=False
            ) as client:
                response = client.post(
                    url, json=payload, headers=self._auth_headers(referer=referer)
                )

        # Anything that isn't a 2xx is a failure, redirects very much included.
        # An unauthenticated write gets a 302 towards the login page with an empty
        # body; treating that as success would record a submission that never
        # happened, which is the worst possible outcome here.
        if response.status_code >= 300:
            body = response.text[:2000]
            raise TransferRejected(
                f"POST {url} failed with {response.status_code}"
                + (
                    f" (redirected to {response.headers.get('location', '?')} -- the session "
                    "is probably dead)"
                    if response.status_code < 400
                    else f": {body}"
                ),
                status_code=response.status_code,
                body=body,
            )

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text[:500]}

    # ------------------------------------------------------------------ reads
    def bootstrap(self, *, refresh: bool = False) -> Bootstrap:
        """``bootstrap-static``: players, teams, gameweeks. Cached per client."""
        if self._bootstrap is not None and not refresh:
            return self._bootstrap
        data = self._get_json(f"{self.settings.fpl_api_base}/bootstrap-static/")
        self._bootstrap = parse_bootstrap(data)
        return self._bootstrap

    def fixtures(self, *, event: int | None = None) -> list[Fixture]:
        url = f"{self.settings.fpl_api_base}/fixtures/"
        if event is not None:
            url += f"?event={event}"
        return [Fixture.model_validate(f) for f in self._get_json(url)]

    def player_summary(self, element_id: int) -> dict:
        """Per-player history and upcoming fixtures."""
        return self._get_json(f"{self.settings.fpl_api_base}/element-summary/{element_id}/")

    def me(self) -> dict:
        return self._get_json(f"{self.settings.fpl_api_base}/me/", authorised=True)

    def my_team(self, entry_id: int | None = None) -> MyTeam:
        entry = entry_id or self.settings.fpl_entry_id
        if not entry:
            raise FPLApiError("No FPL entry id configured (FPL_ENTRY_ID).")
        data = self._get_json(f"{self.settings.fpl_api_base}/my-team/{entry}/", authorised=True)
        return parse_my_team(data)

    def entry(self, entry_id: int | None = None) -> dict:
        entry = entry_id or self.settings.fpl_entry_id
        return self._get_json(f"{self.settings.fpl_api_base}/entry/{entry}/")

    # ----------------------------------------------------------------- writes
    def submit_transfers(
        self,
        *,
        transfers: list[dict],
        event: int,
        entry_id: int | None = None,
        chip: str | None = None,
    ) -> dict:
        """POST to ``/api/transfers/``.

        Each transfer dict must carry ``element_in``, ``element_out``,
        ``purchase_price`` (the incoming player's *current* ``now_cost``) and
        ``selling_price`` (from your own ``my-team`` pick, which may differ from
        ``now_cost`` because of the 50%-of-rise sell-on rule).
        """
        entry = entry_id or self.settings.fpl_entry_id
        payload = {
            "confirmed": True,
            "entry": entry,
            "event": event,
            "transfers": transfers,
            "chip": chip,
            "freehit": chip == "freehit",
            "wildcard": chip == "wildcard",
        }
        if self.settings.dry_run:
            logger.warning("DRY RUN -- not submitting transfers. Payload: %s", payload)
            return {"dry_run": True, "payload": payload}

        logger.info("Submitting %d transfer(s) for GW%d.", len(transfers), event)
        return self._post_json(
            f"{self.settings.fpl_api_base}/transfers/",
            payload,
            referer="https://fantasy.premierleague.com/transfers",
        )

    def submit_picks(
        self,
        *,
        picks: list[dict],
        entry_id: int | None = None,
        chip: str | None = None,
    ) -> dict:
        """POST to ``/api/my-team/{entry}/`` -- captaincy, vice, bench order.

        ``picks`` must be all 15 slots: ``{element, position, is_captain,
        is_vice_captain}``. Position 1-11 is the starting XI, 12-15 the bench in
        auto-sub order (12 is the reserve keeper).
        """
        entry = entry_id or self.settings.fpl_entry_id
        payload: dict[str, Any] = {"picks": picks, "chip": chip}
        if self.settings.dry_run:
            logger.warning("DRY RUN -- not submitting picks. Payload: %s", payload)
            return {"dry_run": True, "payload": payload}

        logger.info("Submitting picks for entry %s.", entry)
        return self._post_json(
            f"{self.settings.fpl_api_base}/my-team/{entry}/",
            payload,
            referer="https://fantasy.premierleague.com/my-team",
        )

    # ------------------------------------------------------------------ utils
    def verify_session(self) -> bool:
        """Cheap check that our cookies still authenticate."""
        try:
            self.me()
            return True
        except (FPLApiError, FPLAuthError) as exc:
            logger.warning("Session verification failed: %s", exc)
            return False


# --------------------------------------------------------------------- parsing
# Kept as module-level functions, not methods, so tests can feed them recorded
# JSON fixtures and exercise exactly the code that runs in production.

# Free transfers when FPL reports no limit (wildcard / pre-season): effectively
# unlimited, and 15 is the most any legal set of transfers can use.
UNLIMITED_FREE_TRANSFERS = 15


def parse_bootstrap(data: dict) -> Bootstrap:
    return Bootstrap(
        players=[Player.model_validate(e) for e in data["elements"]],
        teams=[Team.model_validate(t) for t in data["teams"]],
        events=[_gameweek(e) for e in data["events"]],
    )


def parse_my_team(data: dict) -> MyTeam:
    transfers = data.get("transfers") or {}
    chips = data.get("chips") or []
    limit = transfers.get("limit")
    return MyTeam(
        picks=[Pick.model_validate(p) for p in data.get("picks", [])],
        bank=transfers.get("bank", 0) or 0,
        total_budget=transfers.get("value", 0) or 0,
        # `limit` is None when transfers are unlimited (wildcard / pre-season).
        free_transfers=limit if limit is not None else UNLIMITED_FREE_TRANSFERS,
        chips_available=[c["name"] for c in chips if c.get("status_for_entry") == "available"],
        active_chip=next(
            (c["name"] for c in chips if c.get("status_for_entry") == "active"), None
        ),
    )


def _gameweek(raw: dict) -> Gameweek:
    return Gameweek.model_validate(
        {
            "id": raw["id"],
            "name": raw["name"],
            "deadline_time": raw["deadline_time"],
            "is_current": raw.get("is_current", False),
            "is_next": raw.get("is_next", False),
            "finished": raw.get("finished", False),
        }
    )
