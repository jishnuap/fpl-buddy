"""Typed views over the bits of the FPL API we actually use."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

POSITION_BY_ELEMENT_TYPE = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_REQUIREMENTS = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
MAX_PER_CLUB = 3
SQUAD_SIZE = 15


class Player(BaseModel):
    """One row of ``bootstrap-static.elements``, trimmed."""

    id: int
    web_name: str
    first_name: str = ""
    second_name: str = ""
    team: int
    element_type: int
    now_cost: int
    total_points: int = 0
    form: float = 0.0
    points_per_game: float = 0.0
    selected_by_percent: float = 0.0
    minutes: int = 0
    status: str = "a"  # a=available d=doubtful i=injured s=suspended u=unavailable n=not in squad
    chance_of_playing_next_round: int | None = None
    news: str = ""
    cost_change_event: int = 0

    @property
    def position(self) -> str:
        return POSITION_BY_ELEMENT_TYPE.get(self.element_type, "UNK")

    @property
    def price(self) -> float:
        return self.now_cost / 10

    @property
    def is_flagged(self) -> bool:
        """Injured, suspended, or otherwise not a safe pick."""
        if self.status != "a":
            return True
        return self.chance_of_playing_next_round is not None and (
            self.chance_of_playing_next_round < 75
        )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.second_name}".strip() or self.web_name


class Team(BaseModel):
    id: int
    name: str
    short_name: str
    strength_overall_home: int = 0
    strength_overall_away: int = 0


class Gameweek(BaseModel):
    id: int
    name: str
    deadline_time: datetime
    is_current: bool = False
    is_next: bool = False
    finished: bool = False


class Fixture(BaseModel):
    id: int
    event: int | None = None
    team_h: int
    team_a: int
    team_h_difficulty: int = 3
    team_a_difficulty: int = 3
    kickoff_time: datetime | None = None
    finished: bool = False


class Pick(BaseModel):
    """A squad slot from ``/api/my-team/{entry}/``."""

    element: int
    position: int  # 1-11 starting XI, 12-15 bench
    is_captain: bool = False
    is_vice_captain: bool = False
    selling_price: int = 0
    purchase_price: int = 0
    multiplier: int = 1

    @property
    def is_starter(self) -> bool:
        return self.position <= 11


class MyTeam(BaseModel):
    picks: list[Pick] = Field(default_factory=list)
    bank: int = 0  # tenths of a million
    total_budget: int = 0
    free_transfers: int = 1
    chips_available: list[str] = Field(default_factory=list)
    active_chip: str | None = None

    @property
    def bank_millions(self) -> float:
        return self.bank / 10

    def pick_for(self, element_id: int) -> Pick | None:
        return next((p for p in self.picks if p.element == element_id), None)

    @property
    def captain_id(self) -> int | None:
        return next((p.element for p in self.picks if p.is_captain), None)

    @property
    def vice_captain_id(self) -> int | None:
        return next((p.element for p in self.picks if p.is_vice_captain), None)


class Bootstrap(BaseModel):
    players: list[Player]
    teams: list[Team]
    events: list[Gameweek]

    def player(self, element_id: int) -> Player | None:
        return next((p for p in self.players if p.id == element_id), None)

    def team(self, team_id: int) -> Team | None:
        return next((t for t in self.teams if t.id == team_id), None)

    @property
    def next_gameweek(self) -> Gameweek | None:
        nxt = next((e for e in self.events if e.is_next), None)
        if nxt:
            return nxt
        # Mid-season the flags can lag; fall back to the first unfinished event.
        return next((e for e in self.events if not e.finished), None)

    @property
    def current_gameweek(self) -> Gameweek | None:
        return next((e for e in self.events if e.is_current), None)
