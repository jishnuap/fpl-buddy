"""The daily walk: discover, skip what we have, fetch, extract, summarise, store.

Every step is allowed to fail without taking the run with it. A source that
redesigns its HTML, a feed that 500s, a model call that times out -- each costs
one article or one source, never the harvest, and never the gameweek. The
scheduler treats this job as strictly optional enrichment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..fpl.models import Bootstrap
from .discover import discover
from .extract import extract
from .fetch import Fetcher
from .sources import Source, load_sources
from .store import ArticleNote, KnowledgeStore, content_hash, make_id
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

    def summary(self) -> str:
        return (
            f"{self.stored} new article(s) from {self.considered} candidate(s); "
            f"{self.skipped_known} already known, {self.partial} paywalled, "
            f"{self.pruned} pruned, {len(self.failures)} failure(s)"
        )


def knowledge_dir(settings: Settings) -> Path:
    return Path(settings.state_dir) / "knowledge"


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
    known = store.known_urls()

    for source in config.active:
        try:
            _harvest_source(source, settings, fetcher, store, known, report, bootstrap, model)
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

        response = fetcher.get(candidate.url, source=source)
        if not response.ok:
            if response.status not in (304, 999):
                report.failures.append(f"{source.name}: {candidate.url} -> {response.status}")
            continue
        report.fetched += 1

        article = extract(response.text, candidate.url)
        if article is None or not article.usable:
            logger.info("Nothing usable extracted from %s.", candidate.url)
            continue

        digest = content_hash(article.text)
        if digest in known.values():
            report.skipped_known += 1
            continue

        summary = summarize(article.title, article.text, settings, model=model)
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
        if article.access == "partial":
            reason = "paywalled"
        elif summary.truncated:
            reason = (
                "longer than the summariser's input budget"
                if len(article.text) > MAX_INPUT_CHARS
                else "cut off at the source (likely paywalled)"
            )
        else:
            reason = ""
        access = "partial" if reason else "full"
        if reason.startswith("paywalled") or "paywalled" in reason:
            report.partial += 1

        note = ArticleNote(
            id=make_id(source.name, candidate.url, published),
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
            extract=article.text[:1200],
        )
        store.save(note)
        known[candidate.url] = digest
        report.stored += 1
        logger.info("Stored %s (%s).", note.id, access)


def _parse_published(raw: str):
    from .discover import _parse_date

    return _parse_date(raw) if raw else None
