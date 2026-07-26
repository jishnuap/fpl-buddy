"""The arithmetic both drivers share.

``FplScheduler`` turns these times into APScheduler triggers; ``run_tick``
compares them against now. If they ever disagreed about when the propose window
opens, one deployment would behave differently from the other for reasons no
log line would explain -- hence one function, tested once.
"""

from __future__ import annotations

from datetime import timedelta

from fpl_buddy.schedule import plan_for


def test_the_windows_are_measured_back_from_the_live_deadline(bootstrap, settings):
    plan = plan_for(bootstrap, settings)
    gameweek = bootstrap.next_gameweek
    assert gameweek is not None

    assert plan.gameweek == gameweek.id
    assert plan.deadline == gameweek.deadline_time
    assert plan.propose_at == gameweek.deadline_time - timedelta(
        hours=settings.propose_hours_before_deadline
    )
    assert plan.commit_at == gameweek.deadline_time - timedelta(
        minutes=settings.commit_minutes_before_deadline
    )


def test_settings_move_the_windows(bootstrap, settings):
    settings.propose_hours_before_deadline = 12.0
    settings.commit_minutes_before_deadline = 90.0

    plan = plan_for(bootstrap, settings)

    assert plan.deadline is not None and plan.propose_at is not None
    assert plan.deadline - plan.propose_at == timedelta(hours=12)
    assert plan.commit_at == plan.deadline - timedelta(minutes=90)


def test_the_end_of_the_season_is_the_one_state_callers_branch_on(bootstrap, settings):
    for event in bootstrap.events:
        event.finished = True
        event.is_next = False

    plan = plan_for(bootstrap, settings)

    assert plan.season_over
    assert plan.gameweek is None
    assert plan.deadline is None
    assert plan.propose_at is None
    assert plan.commit_at is None


def test_describe_is_readable_and_does_not_crash_at_season_end(bootstrap, settings):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(settings.timezone)
    assert "propose" in plan_for(bootstrap, settings).describe(zone)

    for event in bootstrap.events:
        event.finished = True
        event.is_next = False
    assert "season is over" in plan_for(bootstrap, settings).describe(zone)
