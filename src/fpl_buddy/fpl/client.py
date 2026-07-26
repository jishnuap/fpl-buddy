"""HTTP client for the Fantasy Premier League API (public reads + authorised writes).

Note on CORS: none of the write endpoints are callable from a browser -- FPL's
CORS policy blocks it. That is fine here; this always runs server-side.

Note on the Firecrawl fallback: FPL's edge (Datadome, in front of Varnish) blocks
requests from cloud-provider IP ranges outright -- a 403 with no body, regardless
of headers or cookies, confirmed live against a Cloud Run job. Firecrawl fetches
through its own IP pool rather than ours, so a read that 403s locally can still
succeed through it. It is tried for reads only, on a 403 only, and only when
FIRECRAWL_API_KEY is set and the package is installed -- otherwise this behaves
exactly as it did before, raising the original error.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import Settings
from .auth import FPLAuthenticator, FPLAuthError, SessionCookies
from .models import (
    UNLIMITED_FREE_TRANSFERS,
    Bootstrap,
    Fixture,
    Gameweek,
    MyTeam,
    Pick,
    Player,
    Team,
)

try:
    from firecrawl import Firecrawl
except ImportError:  # pragma: no cover - optional dependency
    Firecrawl = None

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
        # None = not yet tried, False = tried and unavailable, else a live client.
        self._firecrawl: Any = None

    # --------------------------------------------------------------- plumbing
    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.user_agent,
            "Accept": "application/json",
            "Accept-Language": "en-GB,en;q=0.9",
        }

    def _auth_headers(self, *, referer: str) -> dict[str, str]:
        """Build authorised headers against currently-valid credentials.

        The authenticator is asked every time rather than cached on the instance
        for the life of the process. That matters here: the access token lives 8
        hours and this process stays up for weeks, so a session captured at
        startup is dead long before the deadline job runs. Asking each time is
        cheap -- it is a cache read unless the token is actually near expiry.
        """
        session = self.auth.get_session_cookies()
        self._session = session
        headers = self._base_headers()
        headers.update(
            {
                "Cookie": session.as_header(),
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://fantasy.premierleague.com",
                "Referer": referer,
            }
        )
        if session.access_token:
            headers["Authorization"] = f"Bearer {session.access_token}"
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
            headers = self._auth_headers(referer="https://fantasy.premierleague.com/my-team")
            with httpx.Client(timeout=self.settings.http_timeout_seconds) as client:
                response = client.get(url, headers=headers)

        # A 403 here is not an expired session (that was just handled above) --
        # every FPL Cloud Run deployment we've tested gets this from a plain,
        # freshly-authenticated request too. It is FPL's edge blocking the IP
        # itself, so retrying with different headers never helps; only fetching
        # from a different network does.
        if response.status_code == 403:
            fallback = self._firecrawl_get_json(url, headers=headers)
            if fallback is not None:
                return fallback

        # 3xx included deliberately: the API answers reads with 200, so a redirect
        # means we've been bounced to a login page, not that there's data here.
        if response.status_code >= 300:
            raise FPLApiError(
                f"GET {url} failed with {response.status_code}",
                status_code=response.status_code,
                body=response.text[:1000],
            )
        return response.json()

    def _firecrawl_get_json(self, url: str, *, headers: dict[str, str]) -> Any | None:
        """Retry a 403'd read through Firecrawl. ``None`` means "could not help".

        Every failure mode here -- no key, package not installed, the API call
        itself erroring, a non-JSON response -- returns ``None`` rather than
        raising, so the caller falls through to the original 403. A fallback
        that could itself crash the request would be worse than no fallback.
        """
        if self._firecrawl is None:
            self._firecrawl = self._build_firecrawl_client()
        if self._firecrawl is False:
            return None

        try:
            document = self._firecrawl.scrape(url, formats=["rawHtml"], headers=headers)
        except Exception as exc:  # noqa: BLE001 - a failed fallback must not mask the 403
            logger.warning("Firecrawl fallback for %s failed: %s", url, exc)
            return None

        raw = getattr(document, "raw_html", None) or getattr(document, "rawHtml", None)
        if not raw:
            logger.warning("Firecrawl fallback for %s returned no content.", url)
            return None
        try:
            data = json.loads(raw)
        except ValueError as exc:
            logger.warning("Firecrawl fallback for %s returned unparsable content: %s", url, exc)
            return None

        logger.info("GET %s: 403 direct, recovered via Firecrawl.", url)
        return data

    def _build_firecrawl_client(self) -> Any:
        key = self.settings.firecrawl_api_key.get_secret_value()
        if not key:
            return False
        if Firecrawl is None:
            logger.info(
                "FPL request got 403 and FIRECRAWL_API_KEY is set, but firecrawl-py is not "
                "installed (pip install -e '.[firecrawl]'); cannot fall back."
            )
            return False
        try:
            return Firecrawl(api_key=key)
        except Exception as exc:  # noqa: BLE001 - never let fallback setup crash the read
            logger.warning("Could not create a Firecrawl client for the 403 fallback: %s", exc)
            return False

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
        data = self._get_json(f"{self.settings.fpl_api_base}/bootstrap-static/", authorised=True)
        self._bootstrap = parse_bootstrap(data)
        return self._bootstrap

    def fixtures(self, *, event: int | None = None, future: bool = False) -> list[Fixture]:
        """Fixtures for one gameweek (``event``), or every unplayed one (``future``).

        ``future=1`` is what makes multi-gameweek reasoning possible: a transfer
        is judged over the next few weeks, not just the one being submitted.
        """
        url = f"{self.settings.fpl_api_base}/fixtures/"
        if event is not None:
            url += f"?event={event}"
        elif future:
            url += "?future=1"
        return [Fixture.model_validate(f) for f in self._get_json(url)]

    def set_piece_notes(self) -> dict:
        """Official per-club set-piece notes. Placeholder text until the season starts."""
        return self._get_json(f"{self.settings.fpl_api_base}/team/set-piece-notes/")

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
        """Check that our credentials can read the squad.

        Deliberately probes ``/my-team/`` rather than ``/me/``. Since FPL moved
        to OAuth, ``/me/`` answers ``200`` for a cookie jar with no usable access
        token at all, so a check against it reports a healthy session that cannot
        read the squad or submit anything -- the exact false confidence a
        pre-flight check exists to prevent.
        """
        probe = self.my_team if self.settings.fpl_entry_id else self.me
        try:
            probe()
            return True
        except (FPLApiError, FPLAuthError) as exc:
            logger.warning("Session verification failed: %s", exc)
            return False


# --------------------------------------------------------------------- parsing
# Kept as module-level functions, not methods, so tests can feed them recorded
# JSON fixtures and exercise exactly the code that runs in production.

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
