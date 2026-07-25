"""YouTube channels as a knowledge source: find videos, read their transcripts.

Discovery uses the per-channel Atom feed, which is stable and gives the latest
fifteen uploads with ids, titles and publication dates. Transcripts come from
``youtube-transcript-api``, which reads the caption track YouTube already
displays under the player.

Two things worth knowing before reading further.

**This path deliberately sits outside the robots.txt check.** YouTube disallows
both ``/feeds/videos.xml`` and ``/api/`` for generic crawlers, and the transcript
library brings its own HTTP client, so it never passes through ``fetch.py`` at
all. Rather than let that quietly falsify the "we honour robots" property the
rest of the harvester has, a YouTube source must set ``ignore_robots: true`` in
config -- the bypass is a thing you can see and grep for, not an accident.

**A transcript is not an article.** It is two to three times longer than a
written piece (a half-hour video runs to ~28,000 characters), it has no
headings to chunk on, and speech recognition mangles surnames. The length is
handled by giving transcripts their own input budget; the mangling largely
resolves itself, because player ids are matched against the *summary's* names,
which the model spells correctly from context, rather than against raw
captions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

# Which id on a channel page belongs to *that* channel is not obvious, and
# getting it wrong is silent: you harvest somebody else's uploads and everything
# downstream looks perfectly healthy. The first `"channelId"` in the HTML is some
# other channel entirely -- a recommendation, or the owner of an embedded video.
# `@LetsTalkFPL` resolved that way to "Let's Talk Football", a different channel
# whose videos are about international tournaments.
#
# The canonical link is the page stating its own address, with `externalId` as a
# second opinion. Both agreed on both channels tested; neither agreed with the
# naive match.
_CHANNEL_ID_PATTERNS = (
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{20,})"'),
    re.compile(r'"externalId":"(UC[\w-]{20,})"'),
    re.compile(r'<meta itemprop="identifier" content="(UC[\w-]{20,})"'),
)
_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")

# A timestamp marker roughly this often, so the summariser can cite a point in
# the video and a human can jump straight to it.
TIMESTAMP_EVERY_SECONDS = 60


@dataclass
class Transcript:
    video_id: str
    text: str
    language: str = ""
    generated: bool = True
    duration_seconds: float = 0.0

    @property
    def url(self) -> str:
        return WATCH_URL.format(video_id=self.video_id)


def video_id_from_url(url: str) -> str | None:
    """Pull the id out of a watch URL, or accept a bare id."""
    if _VIDEO_ID_RE.match(url.strip()):
        return url.strip()
    match = re.search(r"[?&]v=([\w-]{11})", url) or re.search(r"youtu\.be/([\w-]{11})", url)
    return match.group(1) if match else None


def resolve_channel_id(channel: str, fetch_page) -> str | None:
    """Turn ``@handle``/URL into the ``UC...`` id the feed needs.

    ``fetch_page`` is injected so this stays testable and so the caller decides
    how the page is fetched. A channel page is *not* robots-disallowed, unlike
    the feed it leads to.
    """
    value = channel.strip()
    if value.startswith("UC") and len(value) > 20:
        return value

    match = re.search(r"/channel/(UC[\w-]{20,})", value)
    if match:
        return match.group(1)

    handle = value if value.startswith("http") else f"https://www.youtube.com/{value.lstrip('/')}"
    html = fetch_page(handle)
    if not html:
        logger.warning("Could not load %s to resolve a channel id.", handle)
        return None
    for pattern in _CHANNEL_ID_PATTERNS:
        found = pattern.search(html)
        if found:
            logger.info("Resolved %s to channel %s.", value, found.group(1))
            return found.group(1)

    logger.warning(
        "No canonical channel id in the page at %s. Not falling back to any "
        "UC-looking id on the page: that is how you end up harvesting a "
        "different channel without noticing.",
        handle,
    )
    return None


def feed_url(channel_id: str) -> str:
    return FEED_URL.format(channel_id=channel_id)


def fetch_transcript(video_id: str, *, languages: tuple[str, ...] = ("en",)) -> Transcript | None:
    """The caption track as readable text with periodic timestamp markers.

    Returns None for anything without usable captions -- live streams, music,
    videos with captions disabled -- which is a skip, never an error.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        logger.warning(
            "youtube-transcript-api is not installed (pip install -e '.[youtube]'); "
            "skipping this video."
        )
        return None

    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=list(languages))
    except Exception as exc:  # noqa: BLE001 - one video, never the run
        logger.info("No transcript for %s (%s).", video_id, type(exc).__name__)
        return None

    snippets = list(fetched)
    if not snippets:
        return None

    text = _with_timestamps(snippets)
    last = snippets[-1]
    duration = float(getattr(last, "start", 0.0)) + float(getattr(last, "duration", 0.0))

    return Transcript(
        video_id=video_id,
        text=text,
        language=str(getattr(fetched, "language", "") or ""),
        generated=bool(getattr(fetched, "is_generated", True)),
        duration_seconds=duration,
    )


def _with_timestamps(snippets) -> str:
    """Join caption segments, marking the clock about once a minute."""
    parts: list[str] = []
    next_marker = 0.0
    for snippet in snippets:
        start = float(getattr(snippet, "start", 0.0))
        if start >= next_marker:
            parts.append(f"\n[{_clock(start)}] ")
            next_marker = start + TIMESTAMP_EVERY_SECONDS
        parts.append(str(getattr(snippet, "text", "")).strip())
    text = " ".join(part for part in parts if part)
    return re.sub(r"[ \t]+", " ", text).strip()


def _clock(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
