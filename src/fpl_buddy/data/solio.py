"""Solio Analytics projections.

``https://fpl.solioanalytics.com/api/data/latest.json`` is free and needs no
auth. It returns a set of pre-ranked leaderboards for the upcoming gameweek
rather than a full player table, so treat it as *signal*, not as a complete
dataset -- ``bootstrap-static`` remains the source of truth for prices, IDs and
availability.

Observed shape (fields are optional; the parser tolerates additions/removals):

    {
      "generatedAt": "...", "gameweek": 3, "deadlineIso": "...", "source": "...",
      "topProjected":  [ {player}, ... ],
      "topCaptains":   [ {player}, ... ],
      "topDifferentials": [...], "topGoals": [...], "topAssists": [...],
      "topBonus": [...], "topDefCon": [...],
      "bestCleanSheets": [...], "bestAttackingFixtures": [...],
      "topTransfersIn": [...], "topTransfersOut": [...]
    }

    {player} = {
      "name": "Haaland", "team": "MCI", "position": "FWD", "price": 155,
      "opponents": [{"opponent": "BOU", "isHome": true}],
      "ownership": 73, "prPoints": 7.24, "prGoals": .., "prAssists": ..,
      "prBonusPoints": .., "captainProjPoints": .., "leverage": ..
    }
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field
from rapidfuzz import fuzz, process

from ..config import Settings
from ..fpl.models import Bootstrap, Player

logger = logging.getLogger(__name__)

LEADERBOARD_KEYS = (
    "topProjected",
    "topCaptains",
    "topDifferentials",
    "topGoals",
    "topAssists",
    "topBonus",
    "topDefCon",
    "bestCleanSheets",
    "bestAttackingFixtures",
    "topTransfersIn",
    "topTransfersOut",
)


class Opponent(BaseModel):
    model_config = ConfigDict(extra="allow")

    opponent: str = ""
    isHome: bool | None = None

    def describe(self) -> str:
        if self.isHome is None:
            return self.opponent
        return f"{self.opponent} ({'H' if self.isHome else 'A'})"


class SolioPlayer(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str = ""
    team: str = ""
    position: str = ""
    price: float | None = None  # tenths of a million, as FPL uses
    ownership: float | None = None
    opponents: list[Opponent] = Field(default_factory=list)
    pr_points: float | None = Field(default=None, alias="prPoints")
    pr_goals: float | None = Field(default=None, alias="prGoals")
    pr_assists: float | None = Field(default=None, alias="prAssists")
    pr_bonus: float | None = Field(default=None, alias="prBonusPoints")
    captain_proj_points: float | None = Field(default=None, alias="captainProjPoints")
    leverage: float | None = None

    # Filled in by the joiner below.
    element_id: int | None = None

    def summary(self) -> str:
        fixtures = ", ".join(o.describe() for o in self.opponents) or "?"
        bits = [f"{self.name} ({self.team}, {self.position})"]
        if self.price is not None:
            bits.append(f"£{self.price / 10:.1f}m")
        if self.pr_points is not None:
            bits.append(f"proj {self.pr_points:.2f}")
        if self.captain_proj_points is not None:
            bits.append(f"capt {self.captain_proj_points:.2f}")
        if self.ownership is not None:
            bits.append(f"own {self.ownership:.0f}%")
        bits.append(f"vs {fixtures}")
        if self.element_id:
            bits.append(f"id={self.element_id}")
        return " | ".join(bits)


class SolioSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")

    generated_at: str | None = Field(default=None, alias="generatedAt")
    gameweek: int | None = None
    deadline_iso: str | None = Field(default=None, alias="deadlineIso")
    source: str | None = None
    boards: dict[str, list[SolioPlayer]] = Field(default_factory=dict)

    def board(self, key: str, limit: int | None = None) -> list[SolioPlayer]:
        rows = self.boards.get(key, [])
        return rows[:limit] if limit else rows

    def projection_for(self, element_id: int) -> SolioPlayer | None:
        """First matching row across all boards -- boards overlap heavily."""
        for rows in self.boards.values():
            for row in rows:
                if row.element_id == element_id:
                    return row
        return None

    def render(self, keys: tuple[str, ...] = LEADERBOARD_KEYS, limit: int = 12) -> str:
        """Compact text form for stuffing into an LLM prompt."""
        out: list[str] = [
            f"Solio Analytics snapshot -- GW{self.gameweek}, generated {self.generated_at}",
        ]
        for key in keys:
            rows = self.board(key, limit)
            if not rows:
                continue
            out.append(f"\n## {key}")
            out.extend(f"  {i + 1}. {r.summary()}" for i, r in enumerate(rows))
        return "\n".join(out)


class SolioClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def fetch(self) -> SolioSnapshot:
        with httpx.Client(timeout=self.settings.http_timeout_seconds) as client:
            response = client.get(
                self.settings.solio_url,
                headers={"User-Agent": self.settings.user_agent, "Accept": "application/json"},
            )
        response.raise_for_status()
        return parse_snapshot(response.json())


def parse_snapshot(raw: dict[str, Any]) -> SolioSnapshot:
    boards: dict[str, list[SolioPlayer]] = {}
    for key, value in raw.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            boards[key] = [SolioPlayer.model_validate(row) for row in value]

    snapshot = SolioSnapshot.model_validate(
        {
            "generatedAt": raw.get("generatedAt"),
            "gameweek": raw.get("gameweek"),
            "deadlineIso": raw.get("deadlineIso"),
            "source": raw.get("source"),
            "boards": {},
        }
    )
    snapshot.boards = boards
    return snapshot


# --------------------------------------------------------------------- joining

def _candidate_names(player: Player) -> list[str]:
    return [player.web_name, player.full_name, player.second_name]


def join_to_elements(
    snapshot: SolioSnapshot,
    bootstrap: Bootstrap,
    *,
    min_score: int = 82,
) -> tuple[SolioSnapshot, list[str]]:
    """Attach FPL ``element_id`` to every Solio row.

    Solio identifies players by display name + 3-letter club code; FPL uses
    integer element ids. Match on club first (cheap and exact), then fuzzy-match
    the name within that club, which makes collisions like the several Silvas
    and Sons far less likely. Anything below ``min_score`` is reported back as
    unmatched rather than silently guessed -- a wrong id here would transfer in
    the wrong player.
    """
    by_short_name: dict[str, int] = {t.short_name.upper(): t.id for t in bootstrap.teams}
    players_by_team: dict[int, list[Player]] = {}
    for player in bootstrap.players:
        players_by_team.setdefault(player.team, []).append(player)

    unmatched: list[str] = []
    seen: dict[str, int | None] = {}

    for rows in snapshot.boards.values():
        for row in rows:
            cache_key = f"{row.team}|{row.name}|{row.position}"
            if cache_key in seen:
                row.element_id = seen[cache_key]
                continue

            team_id = by_short_name.get(row.team.upper())
            if team_id is None:
                # Do NOT fall back to a league-wide name search. Surnames repeat
                # across clubs, so a global fuzzy match on an unrecognised club
                # code is exactly how you transfer in the wrong player.
                logger.warning(
                    "Solio row '%s' has club code '%s', which no FPL team uses; "
                    "reporting it as unmatched.", row.name, row.team,
                )
                unmatched.append(f"{row.name} ({row.team}, {row.position})")
                row.element_id = None
                seen[cache_key] = None
                continue

            pool = players_by_team.get(team_id, [])
            if row.position:
                positional = [p for p in pool if p.position == row.position.upper()]
                pool = positional or pool

            match_id = _best_match(row.name, pool, min_score=min_score)
            if match_id is None:
                unmatched.append(f"{row.name} ({row.team}, {row.position})")
            row.element_id = match_id
            seen[cache_key] = match_id

    if unmatched:
        logger.warning("Solio rows without an FPL id: %s", ", ".join(sorted(set(unmatched))))
    return snapshot, sorted(set(unmatched))


def _best_match(name: str, pool: list[Player], *, min_score: int) -> int | None:
    if not name or not pool:
        return None

    index: dict[str, int] = {}
    for player in pool:
        for candidate in _candidate_names(player):
            if candidate:
                index.setdefault(candidate.casefold(), player.id)

    exact = index.get(name.casefold())
    if exact is not None:
        return exact

    result = process.extractOne(name.casefold(), index.keys(), scorer=fuzz.WRatio)
    if result and result[1] >= min_score:
        return index[result[0]]
    return None
