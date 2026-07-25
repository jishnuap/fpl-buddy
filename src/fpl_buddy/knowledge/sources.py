"""Where articles come from, entirely from config.

Sources live in a YAML file rather than environment variables because a source
is a small tree -- roots, regex filters, caps, tags -- and flattening that into
``KNOWLEDGE_SOURCE_1_ROOT_2`` would be worse than the problem it solves. The env
var points at the file; the file is safe to commit because credentials are
referenced by the *name* of an environment variable, never written inline.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# Extra link hops beyond the configured roots. Zero is the right default: a root
# IS the listing page, so its article links are already reachable. Allowing even
# one hop means every nav, tag and pagination link on that page becomes another
# fetch -- on a large WordPress site that is hundreds of requests to find nothing.
DEFAULT_MAX_DEPTH = 0
DEFAULT_MAX_ARTICLES = 15
DEFAULT_TTL_DAYS = 21

# Hard ceiling on pages fetched while crawling one source, whatever the depth
# and link count conspire to produce.
DEFAULT_MAX_PAGES = 12


class Discovery(BaseModel):
    """How to find candidate article URLs for one source."""

    model_config = ConfigDict(extra="forbid")

    feeds: list[str] = Field(
        default_factory=list,
        description="RSS/Atom feeds. Cheapest and most reliable recency signal; tried first.",
    )
    sitemaps: list[str] = Field(
        default_factory=list,
        description="Sitemap URLs. Good for backfill -- they carry lastmod for every post.",
    )
    roots: list[str] = Field(
        default_factory=list,
        description="Listing pages to crawl for article links when there is no feed.",
    )
    include_patterns: list[str] = Field(
        default_factory=list,
        description="A URL must match one of these to count as an article. Empty means any.",
    )
    exclude_patterns: list[str] = Field(default_factory=list)
    max_depth: int = Field(
        default=DEFAULT_MAX_DEPTH,
        ge=0,
        le=2,
        description="Extra link hops beyond the roots. 0 means the roots themselves only.",
    )
    max_articles_per_run: int = Field(default=DEFAULT_MAX_ARTICLES, ge=1, le=200)
    max_pages_per_run: int = Field(
        default=DEFAULT_MAX_PAGES,
        ge=1,
        le=100,
        description="Hard ceiling on listing pages fetched while crawling this source.",
    )

    @field_validator("include_patterns", "exclude_patterns")
    @classmethod
    def _must_compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"bad regex {pattern!r}: {exc}") from exc
        return patterns

    def matches(self, url: str) -> bool:
        if any(re.search(p, url) for p in self.exclude_patterns):
            return False
        if not self.include_patterns:
            return True
        return any(re.search(p, url) for p in self.include_patterns)


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Stable slug; used in article ids and filenames.")
    enabled: bool = True
    base_url: str = Field(default="https://www.youtube.com")
    kind: Literal["article", "youtube"] = Field(
        default="article",
        description=(
            "'article' fetches and extracts web pages. 'youtube' reads a "
            "channel's upload feed and takes the transcript of each video."
        ),
    )
    channel: str | None = Field(
        default=None,
        description="YouTube channel: a @handle, a /channel/UC... URL, or a bare UC... id.",
    )
    ignore_robots: bool = Field(
        default=False,
        description=(
            "Fetch this source even where robots.txt disallows it. Off everywhere "
            "by default. YouTube needs it: both the upload feed and the caption "
            "endpoint are disallowed to crawlers, so a youtube source that leaves "
            "this false will find nothing. Set it deliberately, per source, so the "
            "exception is visible rather than assumed."
        ),
    )
    min_transcript_chars: int = Field(
        default=1500,
        ge=0,
        description="Below this a 'video' is a short or a trailer; skip it.",
    )
    max_transcript_chars: int = Field(
        default=80_000,
        ge=1000,
        description="Above this it is a multi-hour stream, not a tips video.",
    )
    discovery: Discovery = Field(default_factory=Discovery)
    tags: list[str] = Field(default_factory=list)
    trust: str = Field(
        default="unknown",
        description="Free text carried into the frontmatter so the agent can weigh sources.",
    )
    ttl_days: int = Field(
        default=DEFAULT_TTL_DAYS,
        ge=1,
        description="Team news rots fast. Articles older than this drop out of the index.",
    )
    request_delay_seconds: float = Field(default=1.0, ge=0)

    # Credentials are referenced, never inlined, so this file stays committable.
    cookie_env: str | None = Field(
        default=None,
        description=(
            "Name of an env var holding a Cookie header for a subscription you hold. "
            "Without it, paywalled sources yield only their free portion."
        ),
    )

    # Run-local flag so the "cookie not set" note is logged once, not per fetch.
    _warned_missing_cookie: bool = False

    @field_validator("name")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
            raise ValueError("name must be a lowercase slug, e.g. fantasy-football-scout")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> Source:
        """Catch a misconfigured source at load time, not at 5am in the harvest."""
        if self.kind == "youtube":
            if not self.channel:
                raise ValueError(f"source {self.name}: kind 'youtube' needs a channel")
            if not self.ignore_robots:
                logger.warning(
                    "Source %s is a YouTube channel but ignore_robots is false. Both the "
                    "upload feed and the caption endpoint are disallowed by YouTube's "
                    "robots.txt, so this source will find nothing.",
                    self.name,
                )
        elif self.channel:
            raise ValueError(f"source {self.name}: 'channel' only applies to kind 'youtube'")
        return self

    def cookie(self) -> str | None:
        """Resolve the configured credential from the environment, if set.

        Called once per request, so the "not configured" note is logged only the
        first time -- otherwise a run produces one warning line per fetch.
        """
        if not self.cookie_env:
            return None
        value = os.environ.get(self.cookie_env, "").strip()
        if not value:
            if not self._warned_missing_cookie:
                logger.warning(
                    "Source %s references %s for authentication but it is unset; "
                    "paywalled articles will be stored as partial.",
                    self.name,
                    self.cookie_env,
                )
                object.__setattr__(self, "_warned_missing_cookie", True)
            return None
        return value


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[Source] = Field(default_factory=list)

    @property
    def active(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]


def load_sources(path: str | Path | None) -> SourceConfig:
    """Read the source file. A missing file is 'no sources', not an error.

    Harvesting is an optional enrichment: a deployment that never configures it
    must behave exactly as it did before the feature existed.
    """
    if not path:
        return SourceConfig()
    file_path = Path(path)
    if not file_path.exists():
        logger.info("No knowledge source file at %s; harvesting is off.", file_path)
        return SourceConfig()

    raw = yaml.safe_load(file_path.read_text()) or {}
    config = SourceConfig.model_validate(raw)
    duplicates = {s.name for s in config.sources if [x.name for x in config.sources].count(s.name) > 1}
    if duplicates:
        raise ValueError(f"Duplicate source names in {file_path}: {', '.join(sorted(duplicates))}")
    logger.info(
        "Loaded %d knowledge source(s) from %s (%d enabled).",
        len(config.sources), file_path, len(config.active),
    )
    return config
