"""Ways of getting an article's content, tried in order.

Three, each with a different failure mode, so the chain is worth having:

* **Firecrawl** -- a hosted service that renders JavaScript and returns clean
  markdown. Best output, and the only one that can read a site whose content is
  assembled client-side. It is also metered: one credit per page, 1000 a month
  on the free tier, which a daily harvest can exhaust in about six weeks. So it
  is used for *article pages only* -- never feeds, sitemaps, robots.txt or
  listing pages, which are cheap XML and HTML that plain HTTP handles perfectly
  well and which would otherwise burn a third of the budget on nothing.
* **Scrapling** -- a local library whose plain ``Fetcher`` impersonates a real
  browser's TLS fingerprint. No API, no credits, no browser binaries. It gets
  past the bot protection that returns ``403`` to a stock HTTP client, which is
  most of what fails without it.
* **httpx** -- what the harvester already used. Always present, always free,
  and the reason none of the above is required.

Both of the first two are optional installs. Missing packages, missing keys and
exhausted credits all mean "skip to the next one", never an error: harvesting is
enrichment, and it degrades rather than failing.

None of them defeats a paywall. Content withheld server-side is not in the
response for any client to extract. Firecrawl and Scrapling both accept custom
headers, so a subscription cookie configured on a source is passed through --
authenticated access, not a bypass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from .fetch import Fetcher
from .sources import Source

logger = logging.getLogger(__name__)

DEFAULT_ORDER = "firecrawl,scrapling,httpx"


@dataclass
class ArticleContent:
    """What a backend managed to retrieve. ``markdown`` beats ``html`` if set."""

    url: str
    html: str = ""
    markdown: str = ""
    title: str = ""
    author: str = ""
    published: str = ""
    backend: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.markdown.strip() or self.html.strip())


class Backend:
    """Base class. ``available()`` is checked once per run, before any fetching."""

    name = "base"

    def available(self) -> bool:  # pragma: no cover - overridden
        return False

    def fetch(self, url: str, source: Source) -> ArticleContent | None:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------- firecrawl


@dataclass
class FirecrawlBackend(Backend):
    settings: Settings
    name: str = "firecrawl"
    _client: Any = field(default=None, init=False, repr=False)
    _credits_left: int | None = field(default=None, init=False, repr=False)
    _spent: int = field(default=0, init=False, repr=False)

    def available(self) -> bool:
        key = self.settings.firecrawl_api_key.get_secret_value()
        if not key:
            return False
        try:
            from firecrawl import Firecrawl
        except ImportError:
            logger.info(
                "FIRECRAWL_API_KEY is set but firecrawl-py is not installed "
                "(pip install -e '.[firecrawl]'); skipping that backend."
            )
            return False

        try:
            self._client = Firecrawl(api_key=key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create a Firecrawl client: %s", exc)
            return False

        self._credits_left = self._remaining_credits()
        if self._credits_left is not None:
            logger.info("Firecrawl: %s credit(s) remaining.", self._credits_left)
            if self._credits_left <= self.settings.firecrawl_credit_reserve:
                logger.warning(
                    "Firecrawl has %s credit(s) left, at or below the reserve of %s; "
                    "falling back to the other backends for this run.",
                    self._credits_left, self.settings.firecrawl_credit_reserve,
                )
                return False
        return True

    def _remaining_credits(self) -> int | None:
        """Ask the API rather than guessing from a local tally.

        A local counter drifts: it misses usage from other machines and resets
        if state is lost, which is exactly when you would rather it did not.
        """
        try:
            usage = self._client.get_credit_usage()
        except Exception as exc:  # noqa: BLE001 - never block a harvest on this
            logger.info("Could not read Firecrawl credit usage (%s); proceeding.", exc)
            return None
        for attribute in ("remaining_credits", "credits_remaining", "remaining"):
            value = getattr(usage, attribute, None)
            if isinstance(value, int):
                return value
        if isinstance(usage, dict):
            for key in ("remaining_credits", "credits_remaining", "remaining"):
                if isinstance(usage.get(key), int):
                    return int(usage[key])
        return None

    def _budget_exhausted(self) -> bool:
        if self._credits_left is None:
            return False
        return (self._credits_left - self._spent) <= self.settings.firecrawl_credit_reserve

    def fetch(self, url: str, source: Source) -> ArticleContent | None:
        if self._client is None or self._budget_exhausted():
            return None

        headers: dict[str, str] = {}
        cookie = source.cookie()
        if cookie:
            headers["Cookie"] = cookie

        try:
            document = self._client.scrape(
                url,
                # Both formats, because they are good at different things. The
                # HTML goes through the same extractor every other backend uses,
                # which matters more than it sounds: on a site with a busy
                # comment section, `only_main_content` markdown came back at
                # 36k characters of "230 comments" and navigation around a 1.9k
                # article. Firecrawl's job here is to *reach* pages the others
                # cannot -- rendering JavaScript, getting past bot protection --
                # not to decide what an article is. Markdown is the fallback for
                # when there is no HTML to extract from.
                formats=["markdown", "html"],
                only_main_content=True,
                timeout=int(self.settings.http_timeout_seconds * 1000),
                **({"headers": headers} if headers else {}),
            )
        except Exception as exc:  # noqa: BLE001 - fall through to the next backend
            logger.info("Firecrawl could not fetch %s (%s).", url, exc)
            return None

        self._spent += 1
        markdown = _str(document, "markdown")
        html = _str(document, "html") or _str(document, "rawHtml")
        metadata = _get(document, "metadata")
        if not markdown.strip() and not html.strip():
            return None
        return ArticleContent(
            url=url,
            html=html,
            markdown=markdown,
            title=_str(metadata, "title"),
            author=_str(metadata, "author"),
            published=(
                _str(metadata, "publishedTime") or _str(metadata, "published_time")
            ),
            backend=self.name,
        )


# --------------------------------------------------------------------- scrapling


@dataclass
class ScraplingBackend(Backend):
    settings: Settings
    name: str = "scrapling"
    _fetcher: Any = field(default=None, init=False, repr=False)

    def available(self) -> bool:
        try:
            if self.settings.scrapling_stealth:
                from scrapling.fetchers import StealthyFetcher

                self._fetcher = StealthyFetcher
            else:
                # The plain fetcher impersonates a browser's TLS fingerprint
                # without needing any browser binaries -- which is the whole
                # reason it is the default here.
                from scrapling.fetchers import Fetcher

                self._fetcher = Fetcher
        except ImportError:
            logger.debug("scrapling is not installed; skipping that backend.")
            return False
        return True

    def fetch(self, url: str, source: Source) -> ArticleContent | None:
        if self._fetcher is None:
            return None
        try:
            response = self._fetcher.get(
                url, timeout=int(self.settings.http_timeout_seconds)
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("Scrapling could not fetch %s (%s).", url, exc)
            return None

        status = getattr(response, "status", 0)
        if status != 200:
            logger.info("Scrapling got %s for %s.", status, url)
            return None
        html = getattr(response, "html_content", "") or ""
        if not html.strip():
            return None
        return ArticleContent(url=url, html=html, backend=self.name)


# ------------------------------------------------------------------------ httpx


@dataclass
class HttpxBackend(Backend):
    fetcher: Fetcher
    name: str = "httpx"

    def available(self) -> bool:
        return True

    def fetch(self, url: str, source: Source) -> ArticleContent | None:
        response = self.fetcher.get(url, source=source)
        if not response.ok:
            return None
        return ArticleContent(url=response.url, html=response.text, backend=self.name)


# --------------------------------------------------------------------------- #


def build_backends(settings: Settings, fetcher: Fetcher) -> list[Backend]:
    """The configured chain, filtered to the ones that can actually run."""
    known: dict[str, Backend] = {
        "firecrawl": FirecrawlBackend(settings),
        "scrapling": ScraplingBackend(settings),
        "httpx": HttpxBackend(fetcher),
    }

    chain: list[Backend] = []
    for name in (settings.knowledge_fetch_backends or DEFAULT_ORDER).split(","):
        key = name.strip().casefold()
        if not key:
            continue
        backend = known.get(key)
        if backend is None:
            logger.warning("Unknown fetch backend %r; ignoring it.", key)
            continue
        if backend.available():
            chain.append(backend)

    if not chain:
        # Should be unreachable -- httpx is always available -- but a harvest
        # with no way to fetch anything deserves a clearer failure than an
        # empty loop that silently stores nothing.
        logger.warning("No usable fetch backend configured; falling back to httpx.")
        chain.append(HttpxBackend(fetcher))

    logger.info("Article fetch order: %s.", " -> ".join(b.name for b in chain))
    return chain


def fetch_article(
    url: str, source: Source, backends: list[Backend]
) -> ArticleContent | None:
    """First backend that returns something usable wins."""
    for backend in backends:
        content = backend.fetch(url, source)
        if content is not None and content.usable:
            if backend is not backends[0]:
                logger.info("Fetched %s via %s (fallback).", url, backend.name)
            return content
    return None


def _get(obj: Any, name: str) -> Any:
    """Read a field whether the SDK hands back an object or a dict."""
    if obj is None:
        return None
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name)
    return value


def _str(obj: Any, name: str) -> str:
    """As ``_get``, coerced to a string. Kept separate: nesting the two and
    coercing at every level silently turns a metadata object into ``""``."""
    value = _get(obj, name)
    return value if isinstance(value, str) else ""
