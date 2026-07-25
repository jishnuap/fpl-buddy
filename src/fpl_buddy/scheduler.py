"""APScheduler jobs, anchored to the real FPL deadline.

Deadlines are not on a weekly grid -- international breaks, cup weeks and
rescheduled fixtures move them -- so the schedule is re-derived from
``bootstrap-static`` rather than assumed. A daily re-anchor job picks up changes
and rolls onto the next gameweek once one closes.

Late starts are handled deliberately: if the propose window has already passed
when the process boots (a deploy at T-6h, say), propose shortly after startup
instead of skipping the gameweek entirely.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import Settings
from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

PROPOSE_JOB = "propose"
COMMIT_JOB = "commit"
ANCHOR_JOB = "reanchor"
HARVEST_JOB = "harvest"

# How soon after boot to run a propose whose window has already opened.
CATCH_UP_DELAY = timedelta(minutes=2)

# Misfire grace: a job that fires late because the container was restarting
# should still run -- but the commit job re-validates, so a late run is safe.
MISFIRE_GRACE_SECONDS = 15 * 60


class FplScheduler:
    def __init__(self, settings: Settings, orchestrator: Orchestrator | None = None) -> None:
        self.settings = settings
        self.orchestrator = orchestrator or Orchestrator(settings)
        self.timezone = ZoneInfo(settings.timezone)
        self.scheduler = BackgroundScheduler(timezone=self.timezone)

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        self.scheduler.add_job(
            self.reanchor,
            CronTrigger(hour=4, minute=30, timezone=self.timezone),
            id=ANCHOR_JOB,
            replace_existing=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )
        if self.settings.has_knowledge:
            self.scheduler.add_job(
                self.run_harvest,
                CronTrigger(
                    hour=self.settings.knowledge_harvest_hour, minute=0, timezone=self.timezone
                ),
                id=HARVEST_JOB,
                replace_existing=True,
                misfire_grace_time=MISFIRE_GRACE_SECONDS,
            )
            logger.info(
                "Article harvest scheduled daily at %02d:00 %s.",
                self.settings.knowledge_harvest_hour, self.settings.timezone,
            )
        self.scheduler.start()
        self.reanchor()
        logger.info("Scheduler started (timezone %s).", self.settings.timezone)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # -------------------------------------------------------------------- jobs
    def reanchor(self) -> None:
        """Recompute the propose/commit times from the live deadline."""
        try:
            bootstrap = self.orchestrator.client.bootstrap(refresh=True)
        except Exception as exc:  # noqa: BLE001 - a failed refresh must not kill the loop
            logger.error("Could not refresh bootstrap-static to re-anchor: %s", exc)
            return

        gameweek = bootstrap.next_gameweek
        if gameweek is None:
            logger.info("No upcoming gameweek; season is over. Clearing jobs.")
            self._remove(PROPOSE_JOB)
            self._remove(COMMIT_JOB)
            return

        deadline = gameweek.deadline_time
        now = datetime.now(UTC)
        propose_at = deadline - timedelta(hours=self.settings.propose_hours_before_deadline)
        commit_at = deadline - timedelta(minutes=self.settings.commit_minutes_before_deadline)

        logger.info(
            "GW%s deadline %s -- propose %s, commit %s.",
            gameweek.id,
            deadline.astimezone(self.timezone).isoformat(timespec="minutes"),
            propose_at.astimezone(self.timezone).isoformat(timespec="minutes"),
            commit_at.astimezone(self.timezone).isoformat(timespec="minutes"),
        )

        self._schedule_propose(gameweek.id, propose_at, commit_at, now)
        self._schedule_commit(gameweek.id, commit_at, now)

    def run_propose(self) -> None:
        try:
            proposal = self.orchestrator.propose()
            logger.info("Scheduled propose produced %s.", proposal.id)
        except Exception:
            logger.exception("Scheduled propose failed.")

    def run_commit(self) -> None:
        try:
            proposal = self.orchestrator.auto_commit()
            if proposal is not None:
                logger.info("Commit job left %s in %s.", proposal.id, proposal.status.value)
        except Exception:
            logger.exception("Commit job failed; nothing was submitted.")

    def run_harvest(self) -> None:
        """Collect new articles. Strictly optional: it must never affect a deadline."""
        from .knowledge.harvest import harvest

        try:
            bootstrap = self.orchestrator.client.bootstrap()
        except Exception as exc:  # noqa: BLE001 - only needed to resolve player names
            logger.warning("Harvest could not load bootstrap (%s); skipping id resolution.", exc)
            bootstrap = None
        try:
            report = harvest(self.settings, bootstrap=bootstrap)
            logger.info("Harvest: %s", report.summary())
        except Exception:
            logger.exception("Article harvest failed; proposals are unaffected.")

    # ----------------------------------------------------------------- private
    def _schedule_propose(
        self, gameweek: int, propose_at: datetime, commit_at: datetime, now: datetime
    ) -> None:
        if now >= commit_at:
            logger.warning(
                "Too late to propose for GW%s (commit time has passed). Skipping.", gameweek
            )
            self._remove(PROPOSE_JOB)
            return

        if propose_at <= now:
            existing = self.orchestrator.latest(gameweek=gameweek)
            if existing is not None:
                logger.info(
                    "Propose window for GW%s already passed and %s exists; not re-proposing.",
                    gameweek, existing.id,
                )
                self._remove(PROPOSE_JOB)
                return
            propose_at = now + CATCH_UP_DELAY
            logger.info("Propose window already open for GW%s; catching up at %s.",
                        gameweek, propose_at.isoformat(timespec="seconds"))

        self.scheduler.add_job(
            self.run_propose,
            DateTrigger(run_date=propose_at),
            id=PROPOSE_JOB,
            replace_existing=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )

    def _schedule_commit(self, gameweek: int, commit_at: datetime, now: datetime) -> None:
        if commit_at <= now:
            logger.warning(
                "Commit time for GW%s has already passed; not scheduling a late submit.",
                gameweek,
            )
            self._remove(COMMIT_JOB)
            return
        self.scheduler.add_job(
            self.run_commit,
            DateTrigger(run_date=commit_at),
            id=COMMIT_JOB,
            replace_existing=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )

    def _remove(self, job_id: str) -> None:
        job = self.scheduler.get_job(job_id)
        if job is not None:
            job.remove()

    # ------------------------------------------------------------------- debug
    def describe(self) -> list[str]:
        # A job added before the scheduler starts is still pending and has no
        # next_run_time attribute at all, so ask for it defensively.
        out = []
        for job in self.scheduler.get_jobs():
            when = getattr(job, "next_run_time", None)
            out.append(f"{job.id}: next run {when.isoformat() if when else 'pending'}")
        return out
