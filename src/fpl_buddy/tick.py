"""One invocation of the scheduler, as a job that exits.

``scheduler.py`` keeps a process alive so it can fire a job at an exact moment.
That is the only reason this service ever had to run 24x7, and it is ~98% of the
hosting bill for something that does about twenty minutes of real work a month.

This module is the same logic driven from outside: a platform cron runs
``fpl-buddy tick`` every few minutes, and each run asks "what is due?" and does
it. The two drivers share :func:`fpl_buddy.schedule.plan_for`, so they cannot
disagree about when a gameweek's propose and commit windows open.

Why polling rather than enqueueing a task for the exact minute:

* **A missed run costs minutes, not a gameweek.** Delayed-task services are more
  precise and fail worse -- one dropped enqueue and nothing commits, silently.
* **Deadlines move.** International breaks and rescheduled fixtures shift them,
  which is why the schedule is re-derived from ``bootstrap-static`` rather than
  assumed. Re-deriving every tick is the simplest possible version of that.
* **Nothing else to provision.** No queue, no task service, no extra identity.

The cost of polling is bounded by never doing anything expensive unless
something is actually due -- see :mod:`fpl_buddy.ledger` for what is cached and
why, and note that the heavy imports below are all function-local. An idle tick
reads one small JSON file and exits without importing the agent stack.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings
from .ledger import JobLedger, LedgerState, new_owner
from .schedule import Plan

logger = logging.getLogger(__name__)

ANCHOR = "anchor"
PROPOSE = "propose"
COMMIT = "commit"
HARVEST = "harvest"

# How far ahead of the propose window to start re-reading the live deadline
# every tick instead of trusting the cached one. An hour of slack absorbs a
# deadline that moved earlier since the last anchor.
WINDOW_SLACK = timedelta(hours=1)


@dataclass
class TickReport:
    ran: list[str] = field(default_factory=list)
    idle_reason: str = ""
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.errors:
            return f"ran {self.ran or ['nothing']}; failed: {'; '.join(self.errors)}"
        if self.ran:
            return f"ran {', '.join(self.ran)}"
        return f"nothing due ({self.idle_reason})" if self.idle_reason else "nothing due"


def run_tick(
    settings: Settings,
    *,
    now: datetime | None = None,
    ledger: JobLedger | None = None,
    orchestrator=None,
    owner: str | None = None,
) -> TickReport:
    """Do whatever this moment calls for. Safe to call as often as you like."""
    now = now or datetime.now(UTC)
    ledger = ledger or JobLedger(Path(settings.state_dir) / "ledger.json")
    owner = owner or new_owner()

    if not ledger.acquire(owner, now=now):
        return TickReport(idle_reason="another execution holds the lease")
    try:
        return _run(settings, now, ledger, orchestrator)
    finally:
        ledger.release(owner)


# --------------------------------------------------------------------------- #


def _run(settings: Settings, now: datetime, ledger: JobLedger, orchestrator) -> TickReport:
    report = TickReport()
    state = ledger.read()

    harvest_due = _harvest_due(settings, state, now)
    anchor_due = _anchor_due(settings, state, now)

    if not harvest_due and not anchor_due:
        # The cheap exit, and the one taken by the overwhelming majority of
        # ticks. Nothing above this line imported the agent, the FPL client or
        # anything that talks to a network.
        report.idle_reason = _why_idle(state, now)
        return report

    # Everything below needs a real client. Build one Orchestrator and reuse its
    # client throughout: a second FPLClient would mean a second cookie cache,
    # and the FPL refresh token rotates on use -- two of them racing a refresh
    # is how you invalidate your own session.
    if orchestrator is None:
        from .orchestrator import Orchestrator

        orchestrator = Orchestrator(settings)

    if anchor_due:
        plan = _anchor(settings, ledger, orchestrator, now, report)
        if plan is not None:
            _act_on_plan(settings, ledger, orchestrator, plan, now, report)

    if harvest_due:
        _harvest(settings, ledger, orchestrator, now, report)

    if report.errors:
        _notify_errors(settings, ledger, orchestrator, report, now)

    return report


def _anchor(
    settings: Settings, ledger: JobLedger, orchestrator, now: datetime, report: TickReport
) -> Plan | None:
    """Re-read the live deadline and cache it."""
    from .schedule import plan_for

    try:
        bootstrap = orchestrator.client.bootstrap(refresh=True)
    except Exception as exc:  # noqa: BLE001 - a failed refresh must not kill the tick
        logger.error("Could not refresh bootstrap-static: %s", exc)
        report.errors.append(f"anchor: {exc}")
        return None

    plan = plan_for(bootstrap, settings)
    ledger.remember_deadline(plan.deadline)
    ledger.mark_run(ANCHOR, now)
    report.ran.append(ANCHOR)
    logger.info("%s.", plan.describe(ZoneInfo(settings.timezone)))
    return plan


def _act_on_plan(
    settings: Settings,
    ledger: JobLedger,
    orchestrator,
    plan: Plan,
    now: datetime,
    report: TickReport,
) -> None:
    if plan.season_over:
        return
    assert plan.propose_at and plan.commit_at and plan.deadline  # narrows for mypy

    # The propose window runs from propose_at until the commit job takes over.
    # Starting late is fine and deliberate: a first deploy at T-6h should still
    # produce a proposal rather than skip the gameweek.
    if plan.propose_at <= now < plan.commit_at:
        existing = orchestrator.latest(gameweek=plan.gameweek)
        if _needs_proposing(existing, plan):
            _propose(orchestrator, plan, ledger, now, report)
        else:
            logger.debug("GW%s already has a proposal for this deadline.", plan.gameweek)

    # Committing after the deadline would submit into the next gameweek, so the
    # window closes hard at it.
    if plan.commit_at <= now < plan.deadline:
        # Several ticks fall inside this window. auto_commit() is a no-op on a
        # resolved proposal, but it rebuilds the whole context before working
        # that out -- so check the cheap store read first and only pay for the
        # rebuild when there is genuinely something to do.
        existing = orchestrator.latest(gameweek=plan.gameweek)
        if existing is not None and existing.is_terminal:
            logger.debug("GW%s is already %s.", plan.gameweek, existing.status.value)
        else:
            # A missing proposal is no longer a dead end. The propose window is
            # narrow enough that a couple of skipped ticks can miss it entirely,
            # and auto_commit() will produce one rather than lose the gameweek.
            if existing is None:
                logger.warning(
                    "Commit window is open for GW%s with no proposal; auto_commit will "
                    "make one.", plan.gameweek,
                )
            _commit(orchestrator, ledger, now, report)


def _needs_proposing(existing, plan: Plan) -> bool:
    """Is there a proposal built for *this* deadline, or only an older one?

    Gameweek numbers outlive the plans made against them. A GW1 proposal written
    during pre-season testing still answers "does GW1 have a proposal?" a month
    later, so the old existence check let the real propose window pass without
    running the agent -- and the commit job then acted on a plan written against
    a squad that had been rebuilt since. The window, not the gameweek number, is
    what makes a proposal current.

    A stale proposal the human has already *decided on* is left alone: quietly
    replacing something you approved is worse than a stale plan, and the
    executor re-validates against fresh data before any POST regardless.

    Rejection is the exception, and GW2 was lost to treating it as one of those
    decisions. Rejecting discards a plan; it does not decline the gameweek. A
    rejected proposal from an earlier cycle used to satisfy this guard, so the
    propose window skipped the agent -- and because REJECTED is terminal, the
    commit window skipped it too, leaving the gameweek with nothing at all. A
    rejection made *inside* this window still stands: that is a considered no on
    the current plan, and re-proposing over it would be arguing with the human.
    """
    from .decisions.schema import ProposalStatus

    if existing is None:
        return True
    if existing.status not in (ProposalStatus.PENDING, ProposalStatus.REJECTED):
        return False
    return existing.created_at < plan.propose_at


def _propose(orchestrator, plan: Plan, ledger: JobLedger, now: datetime, report: TickReport) -> None:
    logger.info("Propose window is open for GW%s with no current proposal.", plan.gameweek)
    try:
        proposal = orchestrator.propose()
    except Exception as exc:  # noqa: BLE001 - the next tick tries again
        logger.exception("Propose failed.")
        report.errors.append(f"propose: {exc}")
        return
    ledger.mark_run(PROPOSE, now)
    report.ran.append(PROPOSE)
    logger.info("Proposed %s.", proposal.id)


def _commit(orchestrator, ledger: JobLedger, now: datetime, report: TickReport) -> None:
    try:
        proposal = orchestrator.auto_commit()
    except Exception as exc:  # noqa: BLE001 - never retried automatically
        logger.exception("Commit failed; nothing was submitted.")
        report.errors.append(f"commit: {exc}")
        return
    ledger.mark_run(COMMIT, now)
    report.ran.append(COMMIT)
    if proposal is not None:
        logger.info("Commit left %s in %s.", proposal.id, proposal.status.value)


def _notify_errors(
    settings: Settings, ledger: JobLedger, orchestrator, report: TickReport, now: datetime
) -> None:
    """Say so, once -- a stuck failure must not repost every few minutes."""
    from .notify import safe_notify_errors

    signature = "; ".join(sorted(report.errors))
    if not ledger.read().should_notify_error(signature, now=now):
        return
    safe_notify_errors(orchestrator.notifier, report.errors, settings)
    ledger.mark_error_notified(signature, now)


def _harvest(
    settings: Settings, ledger: JobLedger, orchestrator, now: datetime, report: TickReport
) -> None:
    """Collect articles. Strictly optional: it must never affect a deadline."""
    from .knowledge.harvest import harvest

    try:
        bootstrap = orchestrator.client.bootstrap()
    except Exception as exc:  # noqa: BLE001 - only needed to resolve player names
        logger.warning("Harvest could not load bootstrap (%s); skipping id resolution.", exc)
        bootstrap = None

    # Recorded before the run, not after. A harvest that dies halfway must not
    # be retried by every tick for the rest of the day -- it is the one job here
    # that makes outbound requests to sites that would notice.
    ledger.mark_run(HARVEST, now)
    try:
        result = harvest(settings, bootstrap=bootstrap)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Article harvest failed; proposals are unaffected.")
        report.errors.append(f"harvest: {exc}")
        return
    report.ran.append(HARVEST)
    logger.info("Harvest: %s", result.summary())

    from .notify import safe_notify_harvest

    safe_notify_harvest(orchestrator.notifier, result, settings)


# ------------------------------------------------------------------ due-ness


def _anchor_due(settings: Settings, state: LedgerState, now: datetime) -> bool:
    """Should this tick re-read the live deadline?

    Yes when nothing has ever been read, when the last read is stale, or when
    the propose window is close enough that precision starts to matter. Between
    gameweeks that leaves a handful of refreshes a day.

    The interval is checked before the deadline so that a season with no next
    gameweek settles onto the slow cadence instead of re-reading every tick.
    """
    last = state.ran_at(ANCHOR)
    if last is None or now - last >= timedelta(hours=settings.tick_anchor_interval_hours):
        return True

    deadline = state.deadline
    if deadline is None:
        return False

    window_opens = deadline - timedelta(hours=settings.propose_hours_before_deadline) - WINDOW_SLACK
    return now >= window_opens


def _harvest_due(settings: Settings, state: LedgerState, now: datetime) -> bool:
    """Once a day, at or after the configured local hour.

    "At or after" rather than "at": a tick that fires late, or a platform that
    skips a firing, should still harvest that day.
    """
    if not settings.has_knowledge:
        return False

    zone = ZoneInfo(settings.timezone)
    local = now.astimezone(zone)
    if local.hour < settings.knowledge_harvest_hour:
        return False

    last = state.ran_at(HARVEST)
    return last is None or last.astimezone(zone).date() < local.date()


def _why_idle(state: LedgerState, now: datetime) -> str:
    deadline = state.deadline
    if deadline is None:
        return "no upcoming gameweek"
    remaining = (deadline - now).total_seconds()
    if remaining <= 0:
        return "deadline has passed"
    return f"next deadline in {int(remaining // 86400)}d {int(remaining % 86400 // 3600)}h"
