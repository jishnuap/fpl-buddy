"""AIrsenal expected points, read from a file a sidecar container wrote.

`AIrsenal <https://github.com/alan-turing-institute/AIrsenal>`_ is a Bayesian
scoreline + goal-involvement model with a squad optimiser on top. It takes
minutes to fit and drags in jax, so it does not run here: a separate scheduled
container runs it nightly and leaves ``predictions.json`` on the shared volume.
This module reads that file and nothing else. See ``docs/airsenal.md``.

Two things to keep in mind while reading:

**It is signal, like Solio.** ``bootstrap-static`` remains the source of truth
for ids, prices and availability, and ``my-team`` for the squad. A missing or
stale artefact is never fatal -- :func:`load_snapshot` returns ``None`` and the
brief says so.

**The element id is exact.** AIrsenal stores the FPL element id
(``Player.fpl_api_id``), so unlike the Solio join there is no fuzzy name
matching here and no class of wrong-player bugs. Rows the sidecar could not map
are listed in ``unmatched`` without an id, and never appear as players.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import Settings

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSION = 1


class AirsenalPlayer(BaseModel):
    model_config = ConfigDict(extra="allow")

    element_id: int
    name: str = ""
    team: str = ""
    position: str = ""
    # Keyed by gameweek. A gameweek missing from this map means AIrsenal made no
    # prediction for it -- typically a blank -- which is different information
    # from a prediction of zero, and the artefact keeps them distinct.
    points: dict[int, float] = Field(default_factory=dict)

    @field_validator("points", mode="before")
    @classmethod
    def _coerce_gameweek_keys(cls, value: Any) -> Any:
        """JSON object keys are strings; gameweeks are integers."""
        if isinstance(value, dict):
            out: dict[int, float] = {}
            for key, points in value.items():
                try:
                    out[int(key)] = float(points)
                except (TypeError, ValueError):
                    logger.warning(
                        "Dropping unparseable AIrsenal points entry %r: %r", key, points
                    )
            return out
        return value

    @property
    def total(self) -> float:
        return sum(self.points.values())

    def points_in(self, gameweek: int) -> float | None:
        return self.points.get(gameweek)

    def horizon_text(self) -> str:
        """`GW4 6.81 + GW5 5.02` -- the shape of the run, not just its size."""
        return " + ".join(f"GW{gw} {pts:.2f}" for gw, pts in sorted(self.points.items()))

    def summary(self, *, owned: bool = False) -> str:
        bits = [f"{self.name} ({self.team}, {self.position})", f"xP {self.total:.2f}"]
        if len(self.points) > 1:
            bits.append(self.horizon_text())
        bits.append(f"id={self.element_id}")
        # The Solio boards learned this the hard way: a league-wide table whose
        # rows carry an `id=` reads exactly like a squad table, and an agent
        # captained a player it did not own. Same table shape, same marker.
        bits.append("[OWNED]" if owned else "[not owned]")
        return " | ".join(bits)


class AirsenalMove(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    gameweek: int
    # `in` is a keyword, hence the alias.
    players_in: list[int] = Field(default_factory=list, alias="in")
    players_out: list[int] = Field(default_factory=list, alias="out")


class AirsenalTransferPlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    timestamp: str = ""
    points_gain: float = 0.0
    chip_played: str | None = None
    # Always "public_api_last_published" today. It exists so the caveat cannot
    # be lost in transit: AIrsenal rebuilds your squad from the public API, so
    # this plan is computed against your last *published* picks and knows
    # nothing about transfers you already made this week.
    squad_source: str = "unknown"
    moves: list[AirsenalMove] = Field(default_factory=list)

    def render(self, describe: Any = None) -> str:
        def name(element_id: int) -> str:
            label = describe(element_id) if describe else None
            return label or f"id={element_id}"

        lines = [
            f"AIrsenal's own suggested plan, worth {self.points_gain:.2f} predicted points"
            + (f" (chip: {self.chip_played})" if self.chip_played else ""),
            f"  computed {self.timestamp} against: {self.squad_source}",
        ]
        for move in self.moves:
            outs = ", ".join(name(i) for i in move.players_out) or "nobody"
            ins = ", ".join(name(i) for i in move.players_in) or "nobody"
            lines.append(f"  GW{move.gameweek}: sell {outs} -> buy {ins}")
        return "\n".join(lines)


class AirsenalSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 0
    generated_at: datetime | None = None
    airsenal_version: str = ""
    season: str = ""
    prediction_tag: str = ""
    gameweeks: list[int] = Field(default_factory=list)
    players: list[AirsenalPlayer] = Field(default_factory=list)
    transfer_plan: AirsenalTransferPlan | None = None
    unmatched: list[str] = Field(default_factory=list)

    # ---------------------------------------------------------------- derived
    @property
    def age_hours(self) -> float | None:
        if self.generated_at is None:
            return None
        stamp = self.generated_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return (datetime.now(UTC) - stamp).total_seconds() / 3600

    def player(self, element_id: int) -> AirsenalPlayer | None:
        return next((p for p in self.players if p.element_id == element_id), None)

    def points_for(self, element_id: int, gameweek: int | None = None) -> float | None:
        """Expected points: for one gameweek, or over the whole horizon."""
        player = self.player(element_id)
        if player is None:
            return None
        if gameweek is None:
            return player.total
        return player.points_in(gameweek)

    def ranked(self, position: str | None = None) -> list[AirsenalPlayer]:
        pool = self.players
        if position and position.upper() != "ALL":
            pool = [p for p in pool if p.position.upper() == position.upper()]
        return sorted(pool, key=lambda p: -p.total)

    def rank_of(self, element_id: int) -> tuple[int, int] | None:
        """(rank, size) within the player's own position. Context for a number."""
        player = self.player(element_id)
        if player is None:
            return None
        pool = self.ranked(player.position)
        for index, candidate in enumerate(pool, start=1):
            if candidate.element_id == element_id:
                return index, len(pool)
        return None

    # ----------------------------------------------------------------- slicing
    def restricted_to(self, gameweeks: list[int]) -> AirsenalSnapshot:
        """A copy carrying only these gameweeks.

        A snapshot generated before the last gameweek was played still holds a
        column for it. Summing that column into a "next three gameweeks" total
        silently inflates every player who had a good week, so the past is cut
        off rather than carried.
        """
        wanted = set(gameweeks)
        clone = self.model_copy(deep=True)
        clone.gameweeks = [gw for gw in self.gameweeks if gw in wanted]
        players = []
        for player in clone.players:
            player.points = {gw: pts for gw, pts in player.points.items() if gw in wanted}
            if player.points:
                players.append(player)
        clone.players = players
        return clone

    # ---------------------------------------------------------------- render
    def provenance_line(self) -> str:
        age = self.age_hours
        if len(self.gameweeks) > 1:
            span = f"GW{min(self.gameweeks)}-GW{max(self.gameweeks)}"
        elif self.gameweeks:
            span = f"GW{self.gameweeks[0]}"
        else:
            span = "no gameweeks"
        return (
            f"AIrsenal expected points -- {span}, model run "
            + (f"{age:.0f}h ago" if age is not None else "at an unknown time")
            + (f" (v{self.airsenal_version})" if self.airsenal_version else "")
        )

    def render(self, *, limit: int = 20, owned: set[int] | None = None) -> str:
        owned = owned or set()
        lines = [
            self.provenance_line(),
            "A statistical model: it fits scorelines and goal involvements from history and "
            "knows nothing about today's press conference. It is at its best over a run of "
            "fixtures and at its worst on anything that just changed -- an injury, a new "
            "penalty taker, a manager sacked this morning. Weigh it accordingly.",
            "LEAGUE-WIDE, not your squad -- each row is tagged [OWNED] or [not owned].",
            "",
            f"## Top {limit} by expected points over the horizon",
        ]
        for index, player in enumerate(self.ranked()[:limit], start=1):
            lines.append(f"  {index}. {player.summary(owned=player.element_id in owned)}")
        if self.unmatched:
            lines += [
                "",
                "NOTE: these AIrsenal players could not be mapped to an FPL id and are absent "
                "from the table above: " + ", ".join(self.unmatched[:10]),
            ]
        return "\n".join(lines)


# --------------------------------------------------------------------- loading


def snapshot_path(settings: Settings) -> Path:
    if settings.airsenal_snapshot_path:
        return Path(settings.airsenal_snapshot_path)
    return Path(settings.state_dir) / "airsenal" / "predictions.json"


def load_snapshot(
    settings: Settings, *, gameweek: int, horizon: int = 1
) -> tuple[AirsenalSnapshot | None, str]:
    """Read the artefact, or explain why there isn't one.

    Returns ``(snapshot, note)``. ``note`` is a short human sentence that goes
    into the brief either way: an absent model has to read as absent, because
    "no AIrsenal section" and "AIrsenal has no opinion" are very different
    things and the agent cannot tell them apart on its own.

    Never raises. Every failure here is "run without it", exactly as a failed
    Solio fetch is.
    """
    path = snapshot_path(settings)

    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        # The normal state before the sidecar has ever run. Presence of the file
        # is the switch -- there is deliberately no AIRSENAL_ENABLED, for the
        # same reason open_archive() keys on notes existing.
        return None, "AIrsenal predictions were not available for this run (no snapshot file)."
    except OSError as exc:
        logger.warning("Could not read the AIrsenal snapshot at %s: %s", path, exc)
        return None, "AIrsenal predictions were not available for this run (unreadable file)."
    except json.JSONDecodeError as exc:
        logger.warning("AIrsenal snapshot at %s is not valid JSON: %s", path, exc)
        return None, "AIrsenal predictions were not available for this run (corrupt file)."

    try:
        snapshot = AirsenalSnapshot.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - a third party's file must not kill the run
        logger.warning("AIrsenal snapshot at %s did not parse: %s", path, exc)
        return None, "AIrsenal predictions were not available for this run (unrecognised format)."

    if snapshot.schema_version > SUPPORTED_SCHEMA_VERSION:
        logger.warning(
            "AIrsenal snapshot is schema v%s; this build understands v%s. Ignoring it.",
            snapshot.schema_version,
            SUPPORTED_SCHEMA_VERSION,
        )
        return None, (
            f"AIrsenal predictions were ignored: the sidecar wrote schema v"
            f"{snapshot.schema_version} and this build reads v{SUPPORTED_SCHEMA_VERSION}."
        )

    age = snapshot.age_hours
    if age is None or age > settings.airsenal_max_age_hours:
        logger.warning(
            "AIrsenal snapshot is %s old (limit %.0fh); ignoring it.",
            f"{age:.1f}h" if age is not None else "of unknown age",
            settings.airsenal_max_age_hours,
        )
        return None, (
            "AIrsenal predictions were ignored as stale"
            + (f" ({age:.0f}h old)" if age is not None else "")
            + " -- the nightly model run has probably been failing."
        )

    if gameweek not in snapshot.gameweeks:
        logger.warning(
            "AIrsenal snapshot covers %s but GW%s is next; ignoring it.",
            snapshot.gameweeks,
            gameweek,
        )
        return None, (
            f"AIrsenal predictions were ignored: they cover "
            f"{snapshot.gameweeks or 'nothing'} and this is GW{gameweek}."
        )

    wanted = list(range(gameweek, gameweek + max(horizon, 1)))
    sliced = snapshot.restricted_to(wanted)
    dropped = [gw for gw in snapshot.gameweeks if gw not in wanted]
    if dropped:
        logger.info("Dropped AIrsenal gameweeks outside the horizon: %s", dropped)
    return sliced, ""
