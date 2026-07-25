"""Shared fixtures. Nothing here touches the network.

``tests/fixtures/*.json`` are trimmed but API-shaped recordings. The only thing
rewritten at load time is the gameweek deadline: it is pinned relative to *now*
so the suite doesn't rot the moment the real GW4 deadline passes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_buddy.config import Settings
from fpl_buddy.data.context import DecisionContext
from fpl_buddy.data.solio import join_to_elements, parse_snapshot
from fpl_buddy.decisions.schema import AgentProposal, CaptaincyDecision, Proposal, TransferMove
from fpl_buddy.fpl.client import parse_bootstrap, parse_my_team
from fpl_buddy.fpl.models import Bootstrap, Fixture, MyTeam

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# The squad in my-team.json. Handy names so tests read like intent, not ids.
GK_STARTER = 110      # ARS GKP, slot 1
GK_RESERVE = 510      # CHE GKP, slot 12
DEF_ARS = 120         # ARS DEF, slot 2
DEF_INJURED = 320     # LIV DEF, slot 4, status "i" / 25%
MID_VICE = 130        # ARS MID, slot 5, current vice-captain
MID_LIV = 330         # LIV MID, slot 7, selling price below now_cost
MID_BENCH = 530       # CHE MID, slot 15
DEF_BENCH_TOT = 420   # TOT DEF, slot 13
DEF_BENCH_CHE = 520   # CHE DEF, slot 14
FWD_CAPTAIN = 240     # MCI FWD, slot 9, current captain, £14.5m
FWD_LIV = 340         # LIV FWD, slot 10
FWD_TOT = 440         # TOT FWD, slot 11

# Not owned -- transfer targets.
FREE_MID_NEW = 630        # NEW MID, £5.0m, available
FREE_MID_LIV = 333        # LIV MID, available
FREE_MID_TOT = 433        # TOT MID, available
FREE_DEF_NEW = 620        # NEW DEF, available
FREE_GK_NEW = 610         # NEW GKP, available
FREE_FWD_UNAVAILABLE = 640  # NEW FWD, status "u"
FREE_FWD_DOUBTFUL = 641     # NEW FWD, status "d" / 50%
FREE_FWD_EXPENSIVE = 642    # NEW FWD, £13.0m
FREE_DEF_ARS = 121        # ARS DEF -- a fourth Arsenal player, for the club limit
FREE_MID_ARS = 131        # ARS MID -- ditto

NEXT_GAMEWEEK = 4


def load_json(name: str):
    return json.loads((FIXTURE_DIR / name).read_text())


@pytest.fixture
def hours_to_deadline() -> float:
    """Overridable: how far away the next deadline sits."""
    return 36.0


@pytest.fixture
def bootstrap(hours_to_deadline: float) -> Bootstrap:
    boot = parse_bootstrap(load_json("bootstrap-static.json"))
    deadline = datetime.now(UTC) + timedelta(hours=hours_to_deadline)
    for offset, event in enumerate(e for e in boot.events if not e.finished):
        event.deadline_time = deadline + timedelta(days=7 * offset)
    return boot


@pytest.fixture
def my_team() -> MyTeam:
    return parse_my_team(load_json("my-team.json"))


@pytest.fixture
def fixtures_list() -> list[Fixture]:
    return [Fixture.model_validate(f) for f in load_json("fixtures.json")]


@pytest.fixture
def solio(bootstrap: Bootstrap):
    snapshot = parse_snapshot(load_json("solio-latest.json"))
    snapshot, _unmatched = join_to_elements(snapshot, bootstrap)
    return snapshot


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # _env_file=None keeps a developer's real .env out of the test run.
    return Settings(
        _env_file=None,
        fpl_entry_id=999999,
        state_dir=str(tmp_path / ".state"),
        dry_run=True,
        max_points_hit=0,
        approval_secret="test-secret",
        public_base_url="https://fpl.example.test",
        notify_channel="none",
    )


@pytest.fixture
def context(bootstrap, my_team, fixtures_list) -> DecisionContext:
    gameweek = bootstrap.next_gameweek
    assert gameweek is not None and gameweek.id == NEXT_GAMEWEEK
    return DecisionContext(
        gameweek=gameweek,
        my_team=my_team,
        bootstrap=bootstrap,
        fixtures=fixtures_list,
        solio=None,
    )


def make_proposal(**overrides) -> AgentProposal:
    """A proposal that validates clean against the fixture squad.

    Roll the transfer, keep the current armbands, leave the lineup alone -- the
    boring correct answer, which every failure test then perturbs one field of.
    """
    payload: dict = {
        "gameweek": NEXT_GAMEWEEK,
        "captaincy": CaptaincyDecision(
            captain_id=FWD_CAPTAIN,
            vice_captain_id=MID_VICE,
            captain_name="Vasquez",
            vice_captain_name="Hollis",
            reason="Best projected points and a home fixture.",
        ),
        "transfers": [],
        "starting_xi": [],
        "bench_order": [],
        "chip": None,
        "points_hit": 0,
        "confidence": 0.7,
        "summary": "Roll the transfer, keep the armband on Vasquez.",
        "risks": [],
    }
    payload.update(overrides)
    return AgentProposal.model_validate(payload)


def make_transfer(element_out: int, element_in: int, **overrides) -> TransferMove:
    return TransferMove(element_out=element_out, element_in=element_in, **overrides)


def make_stored(agent: AgentProposal, context: DecisionContext, **overrides) -> Proposal:
    payload: dict = {
        "id": "test-proposal",
        "entry_id": 999999,
        "gameweek": agent.gameweek,
        "deadline": context.gameweek.deadline_time,
        "agent": agent,
    }
    payload.update(overrides)
    return Proposal.model_validate(payload)


class FakeClient:
    """Stands in for FPLClient: fixture data in, recorded calls out.

    Any method that would touch the network and isn't stubbed raises, so a test
    that accidentally reaches for one fails loudly instead of hanging.
    """

    def __init__(
        self,
        bootstrap: Bootstrap,
        my_team: MyTeam,
        fixtures: list[Fixture],
        *,
        transfers_error: Exception | None = None,
        picks_error: Exception | None = None,
    ) -> None:
        self._bootstrap = bootstrap
        self._my_team = my_team
        self._fixtures = fixtures
        self.transfers_error = transfers_error
        self.picks_error = picks_error
        self.transfer_calls: list[dict] = []
        self.picks_calls: list[dict] = []
        self.bootstrap_calls = 0

    def bootstrap(self, *, refresh: bool = False) -> Bootstrap:
        self.bootstrap_calls += 1
        return self._bootstrap

    def my_team(self, entry_id: int | None = None) -> MyTeam:
        return self._my_team

    def fixtures(self, *, event: int | None = None) -> list[Fixture]:
        return self._fixtures

    def player_summary(self, element_id: int) -> dict:
        raise AssertionError("player_summary should not be called in this test")

    def submit_transfers(self, *, transfers, event, entry_id=None, chip=None) -> dict:
        self.transfer_calls.append(
            {"transfers": transfers, "event": event, "entry_id": entry_id, "chip": chip}
        )
        if self.transfers_error is not None:
            raise self.transfers_error
        return {"submitted": len(transfers)}

    def submit_picks(self, *, picks, entry_id=None, chip=None) -> dict:
        self.picks_calls.append({"picks": picks, "entry_id": entry_id, "chip": chip})
        if self.picks_error is not None:
            raise self.picks_error
        return {"picks": len(picks)}


@pytest.fixture
def fake_client(bootstrap, my_team, fixtures_list) -> FakeClient:
    return FakeClient(bootstrap, my_team, fixtures_list)


@pytest.fixture
def mock_solio():
    """Serve the Solio fixture over respx so build_context needs no network."""
    import respx

    with respx.mock(assert_all_called=False) as router:
        router.get("https://fpl.solioanalytics.com/api/data/latest.json").respond(
            200, json=load_json("solio-latest.json")
        )
        yield router


def codes(issues) -> set[str]:
    return {i.code for i in issues}


def fatal_codes(issues) -> set[str]:
    return {i.code for i in issues if i.fatal}
