"""The cron-driven scheduler: what it decides to run, and what it leaves alone.

The point of these tests is the decision table. ``run_tick`` is called at a
particular instant with a particular ledger, and the assertion is about *which*
jobs fired -- the jobs themselves are stubbed, because propose and commit are
already tested against real objects elsewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_buddy.ledger import JobLedger, LedgerState
from fpl_buddy.schedule import plan_for
from fpl_buddy.tick import ANCHOR, COMMIT, HARVEST, PROPOSE, run_tick


class FakeClient:
    def __init__(self, bootstrap):
        self._bootstrap = bootstrap
        self.calls = 0

    def bootstrap(self, refresh: bool = False):
        self.calls += 1
        return self._bootstrap


class FakeOrchestrator:
    """Records what was asked of it. Nothing here touches FPL or a model."""

    def __init__(self, bootstrap, *, existing=None):
        self.client = FakeClient(bootstrap)
        self.proposed = 0
        self.committed = 0
        self._existing = existing

    def latest(self, *, gameweek=None):
        return self._existing

    def propose(self):
        self.proposed += 1
        self._existing = _Stub(f"gw-proposal-{self.proposed}")
        return self._existing

    def auto_commit(self):
        self.committed += 1
        return None


class _Stub:
    def __init__(self, id_: str, *, terminal: bool = False) -> None:
        self.id = id_
        self.is_terminal = terminal
        self.status = type("S", (), {"value": "executed" if terminal else "pending"})()


@pytest.fixture
def ledger(tmp_path: Path) -> JobLedger:
    return JobLedger(tmp_path / "ledger.json")


def _at(bootstrap, settings, *, before_deadline: timedelta) -> datetime:
    """An instant a given distance before the fixture's next deadline."""
    plan = plan_for(bootstrap, settings)
    assert plan.deadline is not None
    return plan.deadline - before_deadline


# ----------------------------------------------------------------- the table


def test_far_from_the_deadline_it_anchors_once_then_goes_quiet(bootstrap, settings, ledger):
    """The whole cost argument rests on this: idle ticks must be cheap."""
    now = _at(bootstrap, settings, before_deadline=timedelta(days=5))
    orchestrator = FakeOrchestrator(bootstrap)

    first = run_tick(settings, now=now, ledger=ledger, orchestrator=orchestrator)
    assert first.ran == [ANCHOR]

    second = run_tick(
        settings, now=now + timedelta(minutes=10), ledger=ledger, orchestrator=orchestrator
    )
    assert second.ran == []
    assert "next deadline in" in second.idle_reason
    # The second tick did not re-download bootstrap-static.
    assert orchestrator.client.calls == 1


def test_a_stale_anchor_is_refreshed_even_when_nothing_is_due(bootstrap, settings, ledger):
    now = _at(bootstrap, settings, before_deadline=timedelta(days=5))
    orchestrator = FakeOrchestrator(bootstrap)
    run_tick(settings, now=now, ledger=ledger, orchestrator=orchestrator)

    later = now + timedelta(hours=settings.tick_anchor_interval_hours + 0.5)
    report = run_tick(settings, now=later, ledger=ledger, orchestrator=orchestrator)
    assert report.ran == [ANCHOR]


def test_inside_the_propose_window_it_proposes(bootstrap, settings, ledger):
    now = _at(bootstrap, settings, before_deadline=timedelta(hours=30))
    orchestrator = FakeOrchestrator(bootstrap)

    report = run_tick(settings, now=now, ledger=ledger, orchestrator=orchestrator)

    assert PROPOSE in report.ran
    assert orchestrator.proposed == 1


def test_it_does_not_propose_twice_for_the_same_gameweek(bootstrap, settings, ledger):
    now = _at(bootstrap, settings, before_deadline=timedelta(hours=30))
    orchestrator = FakeOrchestrator(bootstrap)

    run_tick(settings, now=now, ledger=ledger, orchestrator=orchestrator)
    run_tick(settings, now=now + timedelta(minutes=10), ledger=ledger, orchestrator=orchestrator)

    assert orchestrator.proposed == 1


def test_a_late_first_tick_still_proposes(bootstrap, settings, ledger):
    """Deploying at T-6h should produce a proposal, not skip the gameweek."""
    now = _at(bootstrap, settings, before_deadline=timedelta(hours=6))
    orchestrator = FakeOrchestrator(bootstrap)

    report = run_tick(settings, now=now, ledger=ledger, orchestrator=orchestrator)

    assert PROPOSE in report.ran


def test_inside_the_commit_window_it_commits(bootstrap, settings, ledger):
    now = _at(bootstrap, settings, before_deadline=timedelta(minutes=30))
    orchestrator = FakeOrchestrator(bootstrap, existing=_Stub("existing"))

    report = run_tick(settings, now=now, ledger=ledger, orchestrator=orchestrator)

    assert COMMIT in report.ran
    assert orchestrator.committed == 1
    # Past commit_at, proposing would be pointless -- the window has closed.
    assert orchestrator.proposed == 0


def test_an_already_executed_proposal_is_not_rebuilt_every_tick(bootstrap, settings, ledger):
    """auto_commit() rebuilds the whole context before finding nothing to do."""
    now = _at(bootstrap, settings, before_deadline=timedelta(minutes=30))
    orchestrator = FakeOrchestrator(bootstrap, existing=_Stub("done", terminal=True))

    report = run_tick(settings, now=now, ledger=ledger, orchestrator=orchestrator)

    assert COMMIT not in report.ran
    assert orchestrator.committed == 0


def test_after_the_deadline_it_does_not_commit(bootstrap, settings, ledger):
    """Committing here would submit into the *next* gameweek."""
    now = _at(bootstrap, settings, before_deadline=timedelta(minutes=-5))
    orchestrator = FakeOrchestrator(bootstrap, existing=_Stub("existing"))

    run_tick(settings, now=now, ledger=ledger, orchestrator=orchestrator)

    assert orchestrator.committed == 0


def test_a_failed_anchor_is_reported_and_does_not_raise(bootstrap, settings, ledger):
    class Broken(FakeOrchestrator):
        def __init__(self):
            super().__init__(bootstrap)

            def boom(refresh=False):
                raise RuntimeError("FPL is down")

            self.client.bootstrap = boom  # type: ignore[method-assign]

    report = run_tick(settings, now=datetime.now(UTC), ledger=ledger, orchestrator=Broken())

    assert report.ran == []
    assert "FPL is down" in report.errors[0]


# --------------------------------------------------------------------- lease


def test_a_held_lease_stops_a_second_tick(bootstrap, settings, ledger):
    now = _at(bootstrap, settings, before_deadline=timedelta(hours=30))
    ledger.acquire("the-long-running-one", now=now)
    orchestrator = FakeOrchestrator(bootstrap)

    report = run_tick(
        settings, now=now, ledger=ledger, orchestrator=orchestrator, owner="a-later-tick"
    )

    assert report.ran == []
    assert "lease" in report.idle_reason
    assert orchestrator.proposed == 0


def test_an_expired_lease_is_taken_over(bootstrap, settings, ledger):
    """A job killed mid-run must not block the schedule until someone notices."""
    now = _at(bootstrap, settings, before_deadline=timedelta(hours=30))
    ledger.acquire("the-dead-one", now=now - timedelta(hours=2), ttl_seconds=60)
    orchestrator = FakeOrchestrator(bootstrap)

    report = run_tick(
        settings, now=now, ledger=ledger, orchestrator=orchestrator, owner="a-later-tick"
    )

    assert PROPOSE in report.ran


def test_the_lease_is_released_even_when_a_job_raises(bootstrap, settings, ledger):
    class Exploding(FakeOrchestrator):
        def propose(self):
            raise RuntimeError("model refused")

    now = _at(bootstrap, settings, before_deadline=timedelta(hours=30))
    run_tick(settings, now=now, ledger=ledger, orchestrator=Exploding(bootstrap), owner="one")

    assert ledger.read().lease_until == ""


# ------------------------------------------------------------------ harvest


def test_harvest_runs_once_a_day_at_or_after_its_hour(bootstrap, settings, ledger, monkeypatch):
    from zoneinfo import ZoneInfo

    settings.knowledge_sources_file = "sources.yaml"
    settings.knowledge_harvest_hour = 5
    zone = ZoneInfo(settings.timezone)

    runs = []
    monkeypatch.setattr(
        "fpl_buddy.knowledge.harvest.harvest",
        lambda *a, **k: runs.append(1) or type("R", (), {"summary": lambda self: "ok"})(),
    )

    # 07:00 local, well clear of both the propose and commit windows.
    morning = datetime(2026, 1, 5, 7, 0, tzinfo=zone).astimezone(UTC)
    report = run_tick(settings, now=morning, ledger=ledger, orchestrator=FakeOrchestrator(bootstrap))
    assert HARVEST in report.ran

    # Same day, later tick: already done.
    again = run_tick(
        settings,
        now=morning + timedelta(hours=2),
        ledger=ledger,
        orchestrator=FakeOrchestrator(bootstrap),
    )
    assert HARVEST not in again.ran

    # Next day: due again.
    tomorrow = run_tick(
        settings,
        now=morning + timedelta(days=1),
        ledger=ledger,
        orchestrator=FakeOrchestrator(bootstrap),
    )
    assert HARVEST in tomorrow.ran


def test_harvest_is_skipped_before_its_hour(bootstrap, settings, ledger):
    from zoneinfo import ZoneInfo

    settings.knowledge_sources_file = "sources.yaml"
    settings.knowledge_harvest_hour = 5
    before = datetime(2026, 1, 5, 3, 0, tzinfo=ZoneInfo(settings.timezone)).astimezone(UTC)

    report = run_tick(
        settings, now=before, ledger=ledger, orchestrator=FakeOrchestrator(bootstrap)
    )
    assert HARVEST not in report.ran


def test_a_crashed_harvest_is_not_retried_all_day(bootstrap, settings, ledger, monkeypatch):
    """Politeness: the sites being fetched would notice a retry loop."""
    from zoneinfo import ZoneInfo

    settings.knowledge_sources_file = "sources.yaml"
    attempts = []

    def boom(*a, **k):
        attempts.append(1)
        raise RuntimeError("the feed died")

    monkeypatch.setattr("fpl_buddy.knowledge.harvest.harvest", boom)
    morning = datetime(2026, 1, 5, 7, 0, tzinfo=ZoneInfo(settings.timezone)).astimezone(UTC)

    run_tick(settings, now=morning, ledger=ledger, orchestrator=FakeOrchestrator(bootstrap))
    run_tick(
        settings,
        now=morning + timedelta(minutes=10),
        ledger=ledger,
        orchestrator=FakeOrchestrator(bootstrap),
    )

    assert len(attempts) == 1


def test_no_sources_configured_means_no_harvest(bootstrap, settings, ledger):
    settings.knowledge_sources_file = ""
    state = LedgerState()
    assert state.ran_at(HARVEST) is None

    report = run_tick(
        settings, now=datetime.now(UTC), ledger=ledger, orchestrator=FakeOrchestrator(bootstrap)
    )
    assert HARVEST not in report.ran
