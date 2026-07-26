"""What the tick driver remembers between invocations.

A long-lived scheduler keeps this in memory: which jobs it has already fired,
when the next deadline is, and the fact that it is the only one running. A cron
job that exits after twenty seconds keeps none of it, so it goes in one small
JSON file next to the proposals.

Three things live here, and each earns its place:

**Last-run times**, so a daily job stays daily. The harvest window is "at or
after the configured hour", not "exactly at it" -- a tick that is late, or a
platform that skips a firing, must still harvest that day rather than wait for
tomorrow. That only works if the run is recorded.

**The cached deadline**, so that most ticks cost nothing. Between gameweeks
there is no work to do and no reason to download 700KB of ``bootstrap-static``
to prove it; the cached value is enough to decide "not yet" and exit. Inside the
propose window it is ignored and the live deadline is re-read every tick,
because that is exactly when a moved deadline matters.

**A lease**, because a propose run takes minutes and the platform will start the
next scheduled execution regardless. This is a cooperative lease on a shared
filesystem, not a distributed lock -- two processes reading the same instant
could both take it. That race is milliseconds wide against a ten-minute tick,
and the jobs behind it are individually idempotent anyway (a second propose
finds the first one's proposal and declines). The lease is here to stop wasted
work, not to be the thing that guarantees correctness.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Long enough to cover a propose run (agent call plus FPL round trips), short
# enough that a killed job does not block the next tick for long. A job that
# outlives its lease does not fail -- the next tick simply stops waiting for it.
DEFAULT_LEASE_SECONDS = 15 * 60


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Ignoring unparseable timestamp %r in the ledger.", value)
        return None
    # A naive timestamp in the file would compare unpredictably against an
    # aware "now"; treat it as UTC, which is what everything here writes.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class LedgerState:
    last_run: dict[str, str] = field(default_factory=dict)
    next_deadline: str = ""
    lease_until: str = ""
    lease_owner: str = ""

    def ran_at(self, job: str) -> datetime | None:
        return _parse(self.last_run.get(job))

    @property
    def deadline(self) -> datetime | None:
        return _parse(self.next_deadline)

    @property
    def lease_expires(self) -> datetime | None:
        return _parse(self.lease_until)

    def to_json(self) -> str:
        return json.dumps(
            {
                "last_run": self.last_run,
                "next_deadline": self.next_deadline,
                "lease_until": self.lease_until,
                "lease_owner": self.lease_owner,
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> LedgerState:
        data = json.loads(raw)
        return cls(
            last_run=dict(data.get("last_run") or {}),
            next_deadline=str(data.get("next_deadline") or ""),
            lease_until=str(data.get("lease_until") or ""),
            lease_owner=str(data.get("lease_owner") or ""),
        )


class JobLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------- io
    def read(self) -> LedgerState:
        """The stored state, or a blank one. A damaged file is never fatal.

        Losing the ledger costs one redundant bootstrap fetch and possibly one
        repeated harvest. Refusing to run because a JSON file got truncated
        would cost a gameweek, so a parse failure resets rather than raises.
        """
        try:
            return LedgerState.from_json(self.path.read_text())
        except FileNotFoundError:
            return LedgerState()
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("Ledger at %s is unreadable (%s); starting fresh.", self.path, exc)
            return LedgerState()

    def write(self, state: LedgerState) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(state.to_json())
            tmp.replace(self.path)

    def update(self, **changes) -> LedgerState:
        state = self.read()
        for key, value in changes.items():
            setattr(state, key, value)
        self.write(state)
        return state

    # ---------------------------------------------------------------- leases
    def acquire(
        self, owner: str, *, now: datetime | None = None, ttl_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> bool:
        """Claim the right to act. False means another execution holds it."""
        now = now or datetime.now(UTC)
        state = self.read()
        expires = state.lease_expires
        if expires is not None and expires > now and state.lease_owner != owner:
            logger.info(
                "Another tick (%s) holds the lease until %s; standing down.",
                state.lease_owner or "unknown",
                expires.isoformat(timespec="seconds"),
            )
            return False

        state.lease_owner = owner
        state.lease_until = (now + timedelta(seconds=ttl_seconds)).isoformat()
        self.write(state)
        return True

    def release(self, owner: str) -> None:
        """Give the lease back early so the next tick need not wait it out."""
        state = self.read()
        if state.lease_owner and state.lease_owner != owner:
            # Ours expired and somebody else took it. Leave theirs alone.
            return
        state.lease_owner = ""
        state.lease_until = ""
        self.write(state)

    # ----------------------------------------------------------------- jobs
    def mark_run(self, job: str, when: datetime | None = None) -> None:
        state = self.read()
        state.last_run[job] = (when or datetime.now(UTC)).isoformat()
        self.write(state)

    def remember_deadline(self, deadline: datetime | None) -> None:
        self.update(next_deadline=deadline.isoformat() if deadline else "")


def new_owner() -> str:
    """Something unique per execution, and legible in a log line.

    Both platforms expose an execution id; falling back to the pid keeps this
    working locally and in tests.
    """
    for variable in ("CONTAINER_APP_REPLICA_NAME", "CLOUD_RUN_EXECUTION", "HOSTNAME"):
        value = os.environ.get(variable)
        if value:
            return value
    return f"pid-{os.getpid()}"
