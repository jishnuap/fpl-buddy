"""The daily walk: discover, skip what we have, fetch, extract, summarise, store.

Every step is allowed to fail without taking the run with it. A source that
redesigns its HTML, a feed that 500s, a model call that times out -- each costs
one article or one source, never the harvest, and never the gameweek. The
scheduler treats this job as strictly optional enrichment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import Settings
from ..fpl.models import Bootstrap
from .backends import build_backends, fetch_article
from .discover import discover
from .extract import extract, from_markdown, from_transcript
from .fetch import Fetcher
from .sources import Source, load_sources
from .store import (
    ArticleNote,
    KnowledgeStore,
    content_hash,
    knowledge_dir,
    make_id,
)
from .summarize import MAX_INPUT_CHARS, resolve_players, summarize

logger = logging.getLogger(__name__)


@dataclass
class HarvestReport:
    considered: int = 0
    fetched: int = 0
    stored: int = 0
    skipped_known: int = 0
    partial: int = 0
    pruned: int = 0
    failures: list[str] = field(default_factory=list)
    by_backend: dict[str, int] = field(default_factory=dict)
    # What was actually picked up, not just how much of it. Counts answer "did
    # the harvest work"; this answers "is it finding anything worth reading",
    # which is the question a summary in your pocket is really for.
    notes: list[ArticleNote] = field(default_factory=list)

    def summary(self) -> str:
        backends = ", ".join(f"{n}:{c}" for n, c in sorted(self.by_backend.items()))
        return (
            f"{self.stored} new article(s) from {self.considered} candidate(s); "
            f"{self.skipped_known} already known, {self.partial} paywalled, "
            f"{self.pruned} pruned, {len(self.failures)} failure(s)"
            + (f" [fetched via {backends}]" if backends else "")
        )


def harvest(
    settings: Settings,
    *,
    bootstrap: Bootstrap | None = None,
    model=None,
    store: KnowledgeStore | None = None,
) -> HarvestReport:
    """Run one harvest pass over every enabled source."""
    report = HarvestReport()
    config = load_sources(settings.knowledge_sources_file)
    if not config.active:
        logger.info("No enabled knowledge sources; nothing to harvest.")
        return report

    store = store or KnowledgeStore(knowledge_dir(settings))
    fetcher = Fetcher(settings)
    # Built once per run: availability checks hit the network (a Firecrawl
    # credit lookup), and doing that per source would be wasteful and noisy.
    backends = build_backends(settings, fetcher)
    known = store.known_urls()

    for source in config.active:
        try:
            _harvest_source(
                source, settings, fetcher, backends, store, known, report, bootstrap, model
            )
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
            logger.exception("Harvesting source %s failed.", source.name)
            report.failures.append(f"{source.name}: {exc}")

    report.pruned = store.prune()
    logger.info("Harvest complete: %s", report.summary())
    return report


def _harvest_source(
    source: Source,
    settings: Settings,
    fetcher: Fetcher,
    backends: list,
    store: KnowledgeStore,
    known: dict[str, str],
    report: HarvestReport,
    bootstrap: Bootstrap | None,
    model,
) -> None:
    candidates = discover(source, fetcher)
    report.considered += len(candidates)

    for candidate in candidates:
        if candidate.url in known:
            report.skipped_known += 1
            continue

        if source.kind == "youtube":
            article, video_id = _video_article(candidate, source, report)
            if article is None:
                continue
        else:
            video_id = ""
            content = fetch_article(candidate.url, source, backends)
            if content is None:
                report.failures.append(f"{source.name}: could not fetch {candidate.url}")
                continue
            report.fetched += 1
            report.by_backend[content.backend] = report.by_backend.get(content.backend, 0) + 1

            # HTML first, whichever backend produced it, so every article goes
            # through the same boilerplate removal. Markdown is only used when a
            # backend could not give us HTML at all.
            article = extract(content.html, candidate.url) if content.html else None
            if article is None and content.markdown:
                article = from_markdown(
                    content.markdown,
                    candidate.url,
                    title=content.title,
                    author=content.author,
                    published=content.published,
                )
        if article is None or not article.usable:
            logger.info("Nothing usable extracted from %s.", candidate.url)
            continue

        digest = content_hash(article.text)
        if digest in known.values():
            report.skipped_known += 1
            continue

        summary = summarize(
            article.title,
            article.text,
            settings,
            model=model,
            max_chars=(
                settings.transcript_input_chars if source.kind == "youtube" else None
            ),
        )
        if summary is None:
            report.failures.append(f"{source.name}: could not summarise {candidate.url}")
            continue

        players = (
            resolve_players(summary.player_names, bootstrap) if bootstrap is not None else []
        )
        published = candidate.published or _parse_published(article.published_raw)
        # Three ways to end up with part of an article, and they are not the
        # same thing. Only the publisher's ones are fixable with a subscription.
        #
        # Marker detection alone is not enough: a freemium site may strip its own
        # "restricted to members" notice as boilerplate during extraction, or
        # gate purely with a CSS class, leaving text that simply stops
        # mid-sentence. When the model reports the text as cut off, the length
        # tells us who did the cutting -- short means the source, long means us.
        budget = settings.transcript_input_chars if source.kind == "youtube" else MAX_INPUT_CHARS
        if article.access == "partial":
            reason = "paywalled"
        elif summary.truncated and len(article.text) > budget:
            reason = "longer than the summariser's input budget"
        elif summary.truncated and source.kind != "youtube":
            # A speaker trailing off is not a truncated document, so this only
            # applies to written sources.
            reason = "cut off at the source (likely paywalled)"
        else:
            reason = ""
        access = "partial" if reason else "full"
        if reason.startswith("paywalled") or "paywalled" in reason:
            report.partial += 1

        note = ArticleNote(
            id=make_id(source.name, candidate.url, published, slug=video_id),
            title=article.title,
            url=candidate.url,
            source=source.name,
            summary=summary.summary,
            key_points=summary.key_points,
            author=article.author,
            published=published,
            tags=sorted(set(source.tags) | set(summary.tags)),
            players=players,
            teams=summary.team_names,
            access=access,
            partial_reason=reason,
            trust=source.trust,
            ttl_days=source.ttl_days,
            content_hash=digest,
            kind=source.kind,
            video_id=video_id,
            extract=article.text[:1200],
        )
        store.save(note)
        known[candidate.url] = digest
        report.stored += 1
        report.notes.append(note)
        logger.info("Stored %s (%s).", note.id, access)


def _video_article(candidate, source: Source, report: HarvestReport):
    """Fetch and wrap one video's transcript.

    Length is the filter that matters here. The upload feed carries no duration,
    so a sixty-second short and a two-hour stream arrive looking identical to a
    twenty-minute tips video -- the transcript is the first place their size
    becomes visible, and both extremes are worth skipping.
    """
    from .youtube import fetch_transcript, video_id_from_url

    video_id = video_id_from_url(candidate.url) or ""
    if not video_id:
        return None, ""

    transcript = fetch_transcript(video_id)
    if transcript is None:
        # No captions at all: a live stream, music, or captions switched off.
        return None, video_id
    report.fetched += 1
    report.by_backend["transcript"] = report.by_backend.get("transcript", 0) + 1

    size = len(transcript.text)
    if size < source.min_transcript_chars:
        logger.info("Skipping %s: transcript is %d chars, likely a short.", video_id, size)
        return None, video_id
    if size > source.max_transcript_chars:
        logger.info("Skipping %s: transcript is %d chars, likely a stream.", video_id, size)
        return None, video_id

    return from_transcript(transcript, candidate.title or video_id, author=source.name), video_id


def _parse_published(raw: str):
    from .discover import _parse_date

    return _parse_date(raw) if raw else None
