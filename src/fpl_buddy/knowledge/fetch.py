"""HTTP for crawling, with the manners a daily unattended job needs.

Three things this does that a bare ``httpx.get`` does not:

* honours ``robots.txt`` per host, cached for the run;
* sends ``If-None-Match``/``If-Modified-Since`` so an unchanged article costs a
  ``304`` instead of a download;
* rate-limits per host, because the whole point is to be a good guest on
  someone else's site every single day.

There is deliberately no paywall circumvention here -- no crawler-UA spoofing,
no cache or AMP endpoints. A source that gates content server-side yields its
free portion unless you hold a subscription and supply its cookie through
``Source.cookie_env``, which is authenticated access rather than a bypass.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ..config import Settings
from .sources import Source

logger = logging.getLogger(__name__)

MAX_BYTES = 4_000_000


@dataclass
class Fetched:
    url: str
    status: int
    text: str = ""
    etag: str | None = None
    last_modified: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.text)

    @property
    def unchanged(self) -> bool:
        return self.status == 304


@dataclass
class Fetcher:
    """One instance per harvest run; holds the robots and rate-limit state."""

    settings: Settings
    _robots: dict[str, RobotFileParser | None] = field(default_factory=dict)
    _last_request: dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------ robots
    def _robots_for(self, url: str) -> RobotFileParser | None:
        host = urlparse(url).netloc
        if host in self._robots:
            return self._robots[host]

        parser: RobotFileParser | None = RobotFileParser()
        robots_url = f"{urlparse(url).scheme}://{host}/robots.txt"
        try:
            response = httpx.get(
                robots_url,
                headers={"User-Agent": self.settings.user_agent},
                timeout=self.settings.http_timeout_seconds,
                follow_redirects=True,
            )
            if response.status_code == 200:
                assert parser is not None
                parser.parse(response.text.splitlines())
            else:
                # No robots.txt is permission, per convention.
                parser = None
        except httpx.HTTPError as exc:
            logger.warning("Could not read %s (%s); assuming crawling is allowed.", robots_url, exc)
            parser = None

        self._robots[host] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.settings.user_agent, url)

    # -------------------------------------------------------------- throttling
    def _wait(self, url: str, delay: float) -> None:
        host = urlparse(url).netloc
        previous = self._last_request.get(host)
        if previous is not None:
            elapsed = time.monotonic() - previous
            if elapsed < delay:
                time.sleep(delay - elapsed)
        self._last_request[host] = time.monotonic()

    # ------------------------------------------------------------------- fetch
    def get(
        self,
        url: str,
        *,
        source: Source | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> Fetched:
        # A source may opt out of the robots check, and only per source. It is
        # off by default and has to be written down in config, so the exception
        # is visible rather than a property this code quietly stopped having.
        if not (source is not None and source.ignore_robots) and not self.allowed(url):
            logger.info("robots.txt disallows %s; skipping.", url)
            return Fetched(url=url, status=999)

        headers = {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if source is not None:
            cookie = source.cookie()
            if cookie:
                headers["Cookie"] = cookie

        self._wait(url, source.request_delay_seconds if source else 1.0)

        try:
            response = httpx.get(
                url,
                headers=headers,
                timeout=self.settings.http_timeout_seconds,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            logger.warning("Fetch failed for %s: %s", url, exc)
            return Fetched(url=url, status=0)

        if response.status_code == 304:
            return Fetched(url=url, status=304)
        if response.status_code != 200:
            logger.info("Fetch of %s returned %s.", url, response.status_code)
            return Fetched(url=url, status=response.status_code)

        text = response.text
        if len(text) > MAX_BYTES:
            logger.warning("Truncating oversized response from %s (%d bytes).", url, len(text))
            text = text[:MAX_BYTES]

        return Fetched(
            url=str(response.url),
            status=200,
            text=text,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )
