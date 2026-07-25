"""Typed views over the bits of the FPL API we actually use.

``bootstrap-static`` ships 105 fields per player and we download all of them on
every run, so declaring a field here costs nothing but the line itself. The set
below is the one that changes a decision: set-piece duty, the Opta xG family,
minutes reliability, and FPL's own expected points. Anything genuinely unused
stays undeclared -- ``extra`` is ignored, so the API can add and remove fields
without breaking a run.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

POSITION_BY_ELEMENT_TYPE = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_REQUIREMENTS = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
MAX_PER_CLUB = 3
SQUAD_SIZE = 15

# Free transfers when FPL reports no limit (wildcard / pre-season): effectively
# unlimited, and 15 is the most any legal set of transfers can use. Lives here
# rather than in the client so the brief can say "unlimited" instead of "15" --
# an agent told it has "15 free transfers" reads that as a large but finite
# budget, and rolls. Told they are unlimited, it rebuilds, which is correct.
UNLIMITED_FREE_TRANSFERS = 15


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

    # Set-piece duty: 1 is first choice, None means they are not on the list.
    # The single best cheap predictor of returns for a mid-price attacker.
    penalties_order: int | None = None
    direct_freekicks_order: int | None = None
    corners_and_indirect_freekicks_order: int | None = None

    # Underlying performance. This is Opta xG data, already in the FPL API --
    # which is why there is no Understat scraper in this codebase.
    expected_goals_per_90: float = 0.0
    expected_assists_per_90: float = 0.0
    expected_goal_involvements_per_90: float = 0.0
    expected_goals_conceded_per_90: float = 0.0

    # Minutes reliability. `starts_per_90` is the rotation signal: the prompts
    # ask the agent to reject rotation risks, and this is what it reasons over.
    starts: int = 0
    starts_per_90: float = 0.0

    # FPL's own expected points for the coming gameweek -- a free second opinion
    # to weigh against Solio. Null for a player with nothing to project from.
    ep_next: float | None = None

    # Defensive contribution scoring (tackles, recoveries, CBI).
    defensive_contribution: int = 0
    defensive_contribution_per_90: float = 0.0

    # Market momentum this gameweek: who the herd is moving on.
    transfers_in_event: int = 0
    transfers_out_event: int = 0
    ict_index: float = 0.0

    @field_validator(
        "form",
        "points_per_game",
        "selected_by_percent",
        "expected_goals_per_90",
        "expected_assists_per_90",
        "expected_goal_involvements_per_90",
        "expected_goals_conceded_per_90",
        "starts_per_90",
        "defensive_contribution_per_90",
        "ict_index",
        mode="before",
    )
    @classmethod
    def _null_is_zero(cls, value: object) -> object:
        """FPL sends ``null`` or ``""`` for these on players with no minutes.

        Without this a single pre-season null anywhere in a 558-player payload
        would fail the whole parse, and with it the whole gameweek.
        """
        return 0.0 if value is None or value == "" else value

    @field_validator("ep_next", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if value == "" else value

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

    @property
    def set_piece_duties(self) -> str:
        """Compact set-piece role, e.g. ``P1 C2``. Empty when they take none.

        P = penalties, F = direct free kicks, C = corners and indirect free
        kicks; the number is the club's running order.
        """
        pairs = (
            ("P", self.penalties_order),
            ("F", self.direct_freekicks_order),
            ("C", self.corners_and_indirect_freekicks_order),
        )
        return " ".join(f"{label}{order}" for label, order in pairs if order)


class Team(BaseModel):
    id: int
    name: str
    short_name: str
    strength_overall_home: int = 0
    strength_overall_away: int = 0
    # Attack and defence split out, rather than the single static fixture
    # difficulty rating. All four are 0 until the season starts.
    strength_attack_home: int = 0
    strength_attack_away: int = 0
    strength_defence_home: int = 0
    strength_defence_away: int = 0
    form: float | None = None
    position: int = 0
    played: int = 0
    points: int = 0

    @property
    def has_strength_data(self) -> bool:
        """False pre-season, when FPL has not populated the splits yet."""
        return any(
            (
                self.strength_attack_home,
                self.strength_attack_away,
                self.strength_defence_home,
                self.strength_defence_away,
            )
        )


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

    @property
    def has_unlimited_transfers(self) -> bool:
        """Pre-season, or a wildcard/free-hit gameweek: transfers cost nothing."""
        return self.free_transfers >= UNLIMITED_FREE_TRANSFERS

    @property
    def free_transfers_text(self) -> str:
        if self.has_unlimited_transfers:
            return "unlimited (pre-season or an active wildcard -- transfers are free)"
        return str(self.free_transfers)

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
