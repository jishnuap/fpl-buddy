"""When the propose and commit jobs are due, derived from the live deadline.

This is deliberately a pure function over ``bootstrap-static`` and settings, with
no scheduler, no clock and no I/O. Two very different drivers need the same
answer and must never disagree about it:

* :mod:`fpl_buddy.scheduler` runs in a long-lived process and turns these times
  into APScheduler triggers.
* :mod:`fpl_buddy.tick` runs as a short-lived cron job and compares them against
  "now" on each invocation.

Keeping the arithmetic here means a change to the propose window lands in both
at once, and means it can be tested without starting either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Settings
from .fpl.models import Bootstrap


@dataclass(frozen=True)
class Plan:
    """The two moments that matter for the next gameweek.

    Every field is ``None`` when the season is over, which is the one state the
    callers have to branch on rather than compare against.
    """

    gameweek: int | None
    deadline: datetime | None
    propose_at: datetime | None
    commit_at: datetime | None

    @property
    def season_over(self) -> bool:
        return self.gameweek is None

    def describe(self, timezone) -> str:
        if self.season_over:
            return "No upcoming gameweek; the season is over."
        assert self.deadline and self.propose_at and self.commit_at  # narrows for mypy
        return (
            f"GW{self.gameweek} deadline "
            f"{self.deadline.astimezone(timezone).isoformat(timespec='minutes')} -- "
            f"propose {self.propose_at.astimezone(timezone).isoformat(timespec='minutes')}, "
            f"commit {self.commit_at.astimezone(timezone).isoformat(timespec='minutes')}"
        )


SEASON_OVER = Plan(gameweek=None, deadline=None, propose_at=None, commit_at=None)


def plan_for(bootstrap: Bootstrap, settings: Settings) -> Plan:
    """Work out when to propose and when to commit for the next gameweek."""
    gameweek = bootstrap.next_gameweek
    if gameweek is None:
        return SEASON_OVER

    deadline = gameweek.deadline_time
    return Plan(
        gameweek=gameweek.id,
        deadline=deadline,
        propose_at=deadline - timedelta(hours=settings.propose_hours_before_deadline),
        commit_at=deadline - timedelta(minutes=settings.commit_minutes_before_deadline),
    )
