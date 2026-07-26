"""What the tick driver remembers, and how it behaves when that memory is bad.

The ledger is the only thing standing between a cron job and doing the same work
every ten minutes, so the failure modes matter more than the happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_buddy.ledger import JobLedger, LedgerState, new_owner


@pytest.fixture
def ledger(tmp_path: Path) -> JobLedger:
    return JobLedger(tmp_path / "ledger.json")


def test_a_missing_file_reads_as_empty(ledger):
    state = ledger.read()
    assert state.last_run == {}
    assert state.deadline is None


def test_run_times_and_deadlines_round_trip(ledger):
    when = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
    deadline = datetime(2026, 3, 7, 11, 30, tzinfo=UTC)

    ledger.mark_run("harvest", when)
    ledger.remember_deadline(deadline)

    state = ledger.read()
    assert state.ran_at("harvest") == when
    assert state.deadline == deadline


def test_error_notification_state_round_trips(ledger):
    when = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
    ledger.mark_error_notified("propose: boom", when)

    state = ledger.read()
    assert state.last_error_signature == "propose: boom"
    assert state.last_error_notified_at == when


def test_a_brand_new_ledger_always_says_notify(ledger):
    now = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
    assert ledger.read().should_notify_error("propose: boom", now=now)


def test_a_repeated_error_is_suppressed_within_the_cooldown(ledger):
    first = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
    ledger.mark_error_notified("propose: boom", first)

    later = first + timedelta(minutes=30)
    assert not ledger.read().should_notify_error("propose: boom", now=later)


def test_a_repeated_error_notifies_again_after_the_cooldown(ledger):
    first = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
    ledger.mark_error_notified("propose: boom", first)

    later = first + timedelta(hours=1, seconds=1)
    assert ledger.read().should_notify_error("propose: boom", now=later)


def test_a_different_error_notifies_immediately_even_inside_the_cooldown(ledger):
    first = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
    ledger.mark_error_notified("propose: boom", first)

    moments_later = first + timedelta(seconds=1)
    assert ledger.read().should_notify_error("commit: different failure", now=moments_later)


def test_a_corrupt_file_resets_instead_of_raising(ledger):
    """Losing the ledger costs one redundant fetch. Refusing to run costs a gameweek."""
    ledger.path.write_text("{not json at all")

    state = ledger.read()

    assert state.last_run == {}


def test_an_unparseable_timestamp_is_ignored_not_fatal(ledger):
    ledger.write(LedgerState(last_run={"propose": "the day before yesterday"}))
    assert ledger.read().ran_at("propose") is None


def test_a_naive_timestamp_is_read_as_utc(ledger):
    """Everything writes aware timestamps, but a hand-edited file might not --
    and a naive one would compare unpredictably against an aware "now"."""
    ledger.write(LedgerState(next_deadline="2026-03-07T11:30:00"))

    deadline = ledger.read().deadline

    assert deadline == datetime(2026, 3, 7, 11, 30, tzinfo=UTC)


def test_writes_are_atomic(ledger):
    """A half-written ledger read by a concurrent tick would look corrupt."""
    ledger.mark_run("anchor")
    assert not ledger.path.with_suffix(".tmp").exists()
    assert ledger.read().ran_at("anchor") is not None


# -------------------------------------------------------------------- leases


def test_a_lease_blocks_another_owner(ledger):
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    assert ledger.acquire("first", now=now) is True
    assert ledger.acquire("second", now=now + timedelta(minutes=1)) is False


def test_the_same_owner_can_reacquire(ledger):
    """A retry inside one execution should not deadlock against itself."""
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    assert ledger.acquire("same", now=now) is True
    assert ledger.acquire("same", now=now + timedelta(minutes=1)) is True


def test_an_expired_lease_is_available_again(ledger):
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    ledger.acquire("dead", now=now, ttl_seconds=60)

    assert ledger.acquire("alive", now=now + timedelta(minutes=5)) is True


def test_releasing_frees_it_immediately(ledger):
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    ledger.acquire("first", now=now)
    ledger.release("first")

    assert ledger.acquire("second", now=now) is True


def test_releasing_someone_elses_lease_does_nothing(ledger):
    """Our lease expired and another execution took it. Clearing it on the way
    out would hand a third execution the right to run alongside them."""
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    ledger.acquire("dead", now=now, ttl_seconds=60)
    ledger.acquire("alive", now=now + timedelta(minutes=5))

    ledger.release("dead")

    assert ledger.read().lease_owner == "alive"


def test_owner_ids_are_stable_within_a_process(monkeypatch):
    monkeypatch.delenv("CONTAINER_APP_REPLICA_NAME", raising=False)
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
    monkeypatch.delenv("HOSTNAME", raising=False)

    assert new_owner() == new_owner()
    assert new_owner().startswith("pid-")


def test_a_platform_execution_id_is_preferred(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "tick-abc123")
    assert new_owner() == "tick-abc123"
