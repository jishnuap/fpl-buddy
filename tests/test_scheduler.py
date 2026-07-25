"""Job scheduling around the real deadline.

The scheduler is never started in these tests -- jobs are added to a paused
scheduler and inspected. What matters is *when* things are scheduled, and that
the awkward cases (booting late, deadline already gone, season over) don't end
up either silently doing nothing or submitting at the wrong time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_buddy.decisions.store import FileProposalStore
from fpl_buddy.notify import NullNotifier
from fpl_buddy.orchestrator import Orchestrator
from fpl_buddy.scheduler import COMMIT_JOB, PROPOSE_JOB, FplScheduler

from .fakes import FakeStructuredModel


@pytest.fixture
def orch(settings, tmp_path: Path, fake_client) -> Orchestrator:
    return Orchestrator(
        settings,
        store=FileProposalStore(tmp_path / "proposals"),
        client=fake_client,
        notifier=NullNotifier(),
        model=FakeStructuredModel(),
    )


@pytest.fixture
def scheduler(settings, orch) -> FplScheduler:
    sched = FplScheduler(settings, orchestrator=orch)
    # Started but paused: APScheduler only computes next_run_time once running,
    # and paused means no job body ever actually fires during a test.
    sched.scheduler.start(paused=True)
    yield sched
    sched.shutdown()


def job_times(scheduler: FplScheduler) -> dict[str, datetime]:
    return {
        job.id: job.next_run_time
        for job in scheduler.scheduler.get_jobs()
        if getattr(job, "next_run_time", None) is not None
    }


def set_deadline(client, when: datetime) -> None:
    for event in client.bootstrap().events:
        if not event.finished:
            event.deadline_time = when


# ---------------------------------------------------------------- happy path


def test_jobs_are_anchored_to_the_deadline(scheduler, settings, fake_client):
    deadline = datetime.now(UTC) + timedelta(days=3)
    set_deadline(fake_client, deadline)

    scheduler.reanchor()
    times = job_times(scheduler)

    assert times[PROPOSE_JOB] == deadline - timedelta(
        hours=settings.propose_hours_before_deadline
    )
    assert times[COMMIT_JOB] == deadline - timedelta(
        minutes=settings.commit_minutes_before_deadline
    )


def test_reanchoring_moves_the_jobs(scheduler, fake_client):
    deadline = datetime.now(UTC) + timedelta(days=3)
    set_deadline(fake_client, deadline)
    scheduler.reanchor()
    first = job_times(scheduler)[COMMIT_JOB]

    # A postponed fixture pushes the deadline back a day.
    set_deadline(fake_client, deadline + timedelta(days=1))
    scheduler.reanchor()

    assert job_times(scheduler)[COMMIT_JOB] == first + timedelta(days=1)
    ids = [job.id for job in scheduler.scheduler.get_jobs()]
    assert ids.count(COMMIT_JOB) == 1, "must replace, not duplicate"


def test_reanchor_refreshes_bootstrap_rather_than_trusting_a_cache(scheduler, fake_client):
    scheduler.reanchor()
    assert fake_client.bootstrap_calls >= 1


def test_timezone_comes_from_settings(settings, orch):
    settings.timezone = "Europe/London"
    sched = FplScheduler(settings, orchestrator=orch)
    try:
        assert str(sched.timezone) == "Europe/London"
    finally:
        sched.shutdown()


# ------------------------------------------------------------- awkward cases


def test_booting_inside_the_propose_window_catches_up(scheduler, fake_client):
    """Deployed at T-6h: propose shortly, don't skip the gameweek."""
    set_deadline(fake_client, datetime.now(UTC) + timedelta(hours=6))
    scheduler.reanchor()

    times = job_times(scheduler)
    assert times[PROPOSE_JOB] is not None
    assert times[PROPOSE_JOB] < datetime.now(UTC) + timedelta(minutes=5)
    assert times[COMMIT_JOB] is not None


def test_catch_up_does_not_re_propose_when_one_already_exists(scheduler, fake_client, orch):
    set_deadline(fake_client, datetime.now(UTC) + timedelta(hours=6))
    orch.propose()

    scheduler.reanchor()

    assert PROPOSE_JOB not in job_times(scheduler)
    assert COMMIT_JOB in job_times(scheduler), "still needs to commit what exists"


def test_no_propose_job_once_the_commit_time_has_passed(scheduler, fake_client):
    set_deadline(fake_client, datetime.now(UTC) + timedelta(minutes=10))
    scheduler.reanchor()

    times = job_times(scheduler)
    assert PROPOSE_JOB not in times
    assert COMMIT_JOB not in times, "never schedule a submit for a time already gone"


def test_a_passed_deadline_schedules_nothing(scheduler, fake_client):
    set_deadline(fake_client, datetime.now(UTC) - timedelta(hours=2))
    scheduler.reanchor()
    assert job_times(scheduler) == {}


def test_end_of_season_clears_the_jobs(scheduler, fake_client):
    set_deadline(fake_client, datetime.now(UTC) + timedelta(days=3))
    scheduler.reanchor()
    assert job_times(scheduler)

    for event in fake_client.bootstrap().events:
        event.finished = True
        event.is_next = False
    scheduler.reanchor()

    assert job_times(scheduler) == {}


def test_a_failed_bootstrap_leaves_existing_jobs_alone(scheduler, fake_client):
    set_deadline(fake_client, datetime.now(UTC) + timedelta(days=3))
    scheduler.reanchor()
    before = job_times(scheduler)

    def explode(*_args, **_kwargs):
        raise RuntimeError("FPL is down")

    fake_client.bootstrap = explode
    scheduler.reanchor()

    assert job_times(scheduler) == before, "a transient outage must not unschedule the commit"


# -------------------------------------------------------------- job bodies


def test_run_propose_stores_a_proposal(scheduler, orch):
    scheduler.run_propose()
    assert orch.latest() is not None


def test_run_propose_swallows_failures(scheduler, orch, monkeypatch):
    """A failed propose must not kill the scheduler thread."""

    def explode():
        raise RuntimeError("azure is down")

    monkeypatch.setattr(orch, "propose", explode)
    scheduler.run_propose()  # must not raise


def test_run_commit_submits_an_untouched_proposal(scheduler, orch, fake_client):
    orch.propose()
    scheduler.run_commit()

    from fpl_buddy.decisions.schema import ProposalStatus

    assert orch.latest().status == ProposalStatus.AUTO_EXECUTED
    assert len(fake_client.picks_calls) == 1


def test_run_commit_swallows_failures(scheduler, orch, monkeypatch):
    def explode():
        raise RuntimeError("FPL rejected it")

    monkeypatch.setattr(orch, "auto_commit", explode)
    scheduler.run_commit()  # must not raise


def test_describe_lists_the_jobs(scheduler, fake_client):
    set_deadline(fake_client, datetime.now(UTC) + timedelta(days=3))
    scheduler.reanchor()
    described = " ".join(scheduler.describe())
    assert PROPOSE_JOB in described and COMMIT_JOB in described
