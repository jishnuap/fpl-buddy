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
