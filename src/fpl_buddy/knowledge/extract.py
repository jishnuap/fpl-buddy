"""HTML in, readable article text out -- plus an honest note on what was missing.

``trafilatura`` does the extraction. The part worth writing ourselves is
paywall detection: a freemium site returns ``200`` with a perfectly well-formed
page containing the first fifth of the article and a signup pitch. Treating that
as a complete article means summarising an intro as if it were the analysis, so
the result is labelled ``partial`` and the agent is told which it is.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import trafilatura

logger = logging.getLogger(__name__)

# Too short to be an article, whatever the page claims.
MIN_USABLE_CHARS = 400

# Phrases publishers use where the rest of the article should be. Matched
# case-insensitively against the extracted text.
PAYWALL_MARKERS = (
    "restricted to",
    "members only",
    "members-only",
    "subscribe to read",
    "sign up to read",
    "register to continue",
    "this content is for",
    "become a member",
    "premium members",
    "paid subscribers",
)


@dataclass
class Article:
    url: str
    title: str
    text: str
    author: str = ""
    published_raw: str = ""
    access: str = "full"  # full | partial
    paywall_marker: str = ""

    @property
    def usable(self) -> bool:
        return len(self.text) >= MIN_USABLE_CHARS


def from_markdown(markdown: str, url: str, *, title: str = "", author: str = "",
                  published: str = "") -> Article | None:
    """Wrap markdown a backend already extracted for us.

    Firecrawl returns clean markdown, so running it back through an HTML
    extractor would be lossy for no benefit. The paywall and usability checks
    still apply -- a hosted renderer receives the same truncated page a paywall
    serves everyone else.
    """
    text = _tidy(_strip_markdown(_drop_interstitial(markdown)))
    if not text:
        return None
    marker = _paywall_marker(text)
    return Article(
        url=url,
        title=(title or url).strip(),
        text=text,
        author=author.strip(),
        published_raw=published.strip(),
        access="partial" if marker else "full",
        paywall_marker=marker,
    )


# Text a rendering browser can prepend that a plain HTTP client never sees:
# consent walls, extension blocks, "enable JavaScript" notices. It arrives ahead
# of the real article, so summarising without trimming it spends input budget on
# an error page.
_INTERSTITIAL_MARKERS = (
    "err_blocked_by_client",
    "blocked by an extension",
    "is blocked",
    "enable javascript",
    "accept all cookies",
    "we use cookies",
    "consent",
)
_INTERSTITIAL_SCAN_LINES = 40


def _drop_interstitial(markdown: str) -> str:
    """Cut everything up to the last interstitial line near the top.

    Only the opening lines are examined: an article that merely mentions
    cookies or consent halfway down is an article, not a wall.
    """
    lines = markdown.splitlines()
    cut = -1
    for index, line in enumerate(lines[:_INTERSTITIAL_SCAN_LINES]):
        lowered = line.casefold()
        if any(marker in lowered for marker in _INTERSTITIAL_MARKERS):
            cut = index
    if cut < 0:
        return markdown

    # Whatever follows the notice is usually its furniture -- a "Reload" link, a
    # stray domain. Only leading short lines go, and only once an interstitial
    # has already been found, so a real article's short opening is safe.
    remainder = lines[cut + 1 :]
    while remainder and len(remainder[0].strip()) < 40:
        remainder.pop(0)

    logger.info("Dropped %d interstitial line(s) from rendered markdown.", len(lines) - len(remainder))
    return "\n".join(remainder)


def _strip_markdown(markdown: str) -> str:
    """Drop the syntax, keep the prose.

    The summariser reads this as plain text and links are noise -- a page of
    "[Read more](https://...)" spends input budget on URLs nobody follows.
    """
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", markdown)      # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)        # links -> label
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)   # headings
    text = re.sub(r"[*_`>]+", "", text)                          # emphasis, quotes
    return text


def extract(html: str, url: str) -> Article | None:
    """Pull the article out of a page, or None if there isn't one."""
    if not html.strip():
        return None

    text = trafilatura.extract(
        html,
        url=url,
        favor_precision=True,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    )
    if not text:
        logger.info("No article text extracted from %s.", url)
        return None

    metadata = trafilatura.extract_metadata(html, default_url=url)
    title = (getattr(metadata, "title", None) or _title_from_html(html) or url).strip()
    author = (getattr(metadata, "author", None) or "").strip()
    published = (getattr(metadata, "date", None) or "").strip()

    text = _tidy(text)
    marker = _paywall_marker(text)

    return Article(
        url=url,
        title=title,
        text=text,
        author=author,
        published_raw=published,
        access="partial" if marker else "full",
        paywall_marker=marker,
    )


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _paywall_marker(text: str) -> str:
    haystack = text.casefold()
    for marker in PAYWALL_MARKERS:
        if marker in haystack:
            return marker
    return ""


def _title_from_html(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not match:
        return ""
    import html as html_module

    return html_module.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
