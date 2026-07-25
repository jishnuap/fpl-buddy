"""Turn a source into a list of candidate article URLs.

Three strategies, tried in this order, because they differ enormously in cost
and reliability:

1. **Feeds.** A feed is a publisher telling you what is new, in a stable format.
   One request answers "has anything been published" for the whole site.
2. **Sitemaps.** Every post with a ``lastmod``. Good for backfill, verbose for
   daily use.
3. **Root crawling.** Fetch a listing page, pull the links, keep the ones that
   look like articles. Works anywhere, breaks on redesign -- the fallback, not
   the default.

Whichever is used, the result is filtered through the source's include/exclude
patterns, kept on the source's own host, and capped.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from urllib.parse import urldefrag, urljoin, urlparse

from .fetch import Fetcher
from .sources import Source

logger = logging.getLogger(__name__)

_LINK_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\'#]+)', re.I)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_ITEM_RE = re.compile(r"<(?:item|entry)\b.*?</(?:item|entry)>", re.I | re.S)
_FEED_LINK_RE = re.compile(
    r"<link[^>]*?href=[\"']([^\"']+)[\"']|<link>\s*([^<\s]+)\s*</link>", re.I
)
_PUBDATE_RE = re.compile(r"<(?:pubDate|updated|published)>\s*([^<]+?)\s*</", re.I)


class Candidate:
    """A URL worth considering, plus whatever date the source volunteered."""

    __slots__ = ("url", "published")

    def __init__(self, url: str, published: datetime | None = None) -> None:
        self.url = url
        self.published = published

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Candidate({self.url!r}, {self.published!r})"


def discover(source: Source, fetcher: Fetcher) -> list[Candidate]:
    """Candidate article URLs for one source, deduplicated and capped."""
    seen: dict[str, Candidate] = {}

    for url in source.discovery.feeds:
        for candidate in _from_feed(url, source, fetcher):
            seen.setdefault(candidate.url, candidate)

    for url in source.discovery.sitemaps:
        for candidate in _from_sitemap(url, source, fetcher):
            seen.setdefault(candidate.url, candidate)

    # Crawling is the expensive, fragile path. If the feeds already produced a
    # full run's worth of the newest articles, walking listing pages can only
    # find older ones that would be trimmed anyway -- so don't hit the site.
    if source.discovery.roots:
        if len(seen) >= source.discovery.max_articles_per_run and source.discovery.feeds:
            logger.info(
                "%s: feeds already yielded %d candidate(s); skipping the root crawl.",
                source.name, len(seen),
            )
        else:
            for candidate in _from_roots(source, fetcher):
                seen.setdefault(candidate.url, candidate)

    # Newest first when a date is known; undated candidates go last, since an
    # undated URL from a listing page is usually navigation rather than news.
    ordered = sorted(
        seen.values(),
        key=lambda c: c.published or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    if len(ordered) > source.discovery.max_articles_per_run:
        logger.info(
            "%s: %d candidates found, keeping the newest %d.",
            source.name, len(ordered), source.discovery.max_articles_per_run,
        )
    return ordered[: source.discovery.max_articles_per_run]


# --------------------------------------------------------------------------- #


def _acceptable(url: str, source: Source) -> bool:
    """On the source's own host, and matching its article patterns."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc != urlparse(source.base_url).netloc:
        return False
    return source.discovery.matches(url)


def _clean(base: str, href: str) -> str:
    return urldefrag(urljoin(base, href.strip()))[0].rstrip("/")


def _from_feed(feed_url: str, source: Source, fetcher: Fetcher) -> list[Candidate]:
    response = fetcher.get(feed_url, source=source)
    if not response.ok:
        logger.info("%s: feed %s unavailable (status %s).", source.name, feed_url, response.status)
        return []

    out: list[Candidate] = []
    for block in _ITEM_RE.findall(response.text):
        link = None
        for match in _FEED_LINK_RE.finditer(block):
            link = (match.group(1) or match.group(2) or "").strip()
            if link:
                break
        if not link:
            continue
        url = _clean(feed_url, link)
        if not _acceptable(url, source):
            continue
        stamp = _PUBDATE_RE.search(block)
        out.append(Candidate(url, _parse_date(stamp.group(1)) if stamp else None))

    logger.info("%s: feed %s yielded %d article(s).", source.name, feed_url, len(out))
    return out


def _from_sitemap(sitemap_url: str, source: Source, fetcher: Fetcher) -> list[Candidate]:
    response = fetcher.get(sitemap_url, source=source)
    if not response.ok:
        return []

    out: list[Candidate] = []
    blocks = re.findall(r"<url>.*?</url>", response.text, re.I | re.S) or [response.text]
    for block in blocks:
        loc = _LOC_RE.search(block)
        if not loc:
            continue
        url = _clean(sitemap_url, loc.group(1))
        if not _acceptable(url, source):
            continue
        stamp = re.search(r"<lastmod>\s*([^<\s]+)", block, re.I)
        out.append(Candidate(url, _parse_date(stamp.group(1)) if stamp else None))
    logger.info("%s: sitemap %s yielded %d article(s).", source.name, sitemap_url, len(out))
    return out


def _from_roots(source: Source, fetcher: Fetcher) -> list[Candidate]:
    """Breadth-first from each configured root, within a strict page budget.

    Two limits, and both are load-bearing. ``max_depth`` is the number of extra
    hops past a root, defaulting to zero -- a root is already the listing page,
    and following its other links means fetching every nav, tag and pagination
    target. ``max_pages_per_run`` is the backstop for when a site's link
    structure defeats the patterns anyway.
    """
    out: list[Candidate] = []
    visited: set[str] = set()
    pages_fetched = 0
    budget = source.discovery.max_pages_per_run
    frontier = [(_clean(source.base_url, root), 0) for root in source.discovery.roots]

    while frontier:
        if pages_fetched >= budget:
            logger.info(
                "%s: crawl page budget (%d) reached with %d still queued.",
                source.name, budget, len(frontier),
            )
            break

        page_url, depth = frontier.pop(0)
        if page_url in visited or depth > source.discovery.max_depth:
            continue
        visited.add(page_url)

        response = fetcher.get(page_url, source=source)
        pages_fetched += 1
        if not response.ok:
            continue

        for href in _LINK_RE.findall(response.text):
            url = _clean(page_url, href)
            if url in visited:
                continue
            if _acceptable(url, source):
                out.append(Candidate(url))
                visited.add(url)
            elif depth < source.discovery.max_depth:
                # Not an article, but might list some. Same host only.
                if urlparse(url).netloc == urlparse(source.base_url).netloc:
                    frontier.append((url, depth + 1))

    logger.info(
        "%s: crawled %d page(s) from %d root(s), yielding %d article(s).",
        source.name, pages_fetched, len(source.discovery.roots), len(out),
    )
    return out


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip()
    for parse in (_parse_iso, _parse_rfc2822):
        result = parse(raw)
        if result is not None:
            return result
    return None


def _parse_iso(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_rfc2822(raw: str) -> datetime | None:
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
