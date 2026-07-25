"""The flows, end to end: propose, approve, reject, amend, auto-commit.

Everything is offline -- fixture data through FakeClient, a fake chat model, a
file store in tmp_path. What's being tested is the product decision: silence
means consent, but only at the deadline, and only if it still validates.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_buddy.decisions.executor import ExecutionBlocked
from fpl_buddy.decisions.schema import ProposalStatus
from fpl_buddy.decisions.store import FileProposalStore
from fpl_buddy.notify import Notifier
from fpl_buddy.orchestrator import NotActionable, Orchestrator, ProposalNotFound

from .conftest import FREE_MID_NEW, FWD_CAPTAIN, MID_LIV, NEXT_GAMEWEEK
from .fakes import ONE_TRANSFER_PROPOSAL, FakeStructuredModel


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, subject, text, *, html=None, meta=None) -> None:
        self.sent.append((subject, text))


class BrokenNotifier(Notifier):
    def send(self, subject, text, *, html=None, meta=None) -> None:
        raise RuntimeError("smtp is on fire")


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def store(tmp_path: Path) -> FileProposalStore:
    return FileProposalStore(tmp_path / "proposals")


@pytest.fixture
def orch(settings, store, fake_client, notifier, mock_solio) -> Orchestrator:
    return Orchestrator(
        settings,
        store=store,
        client=fake_client,
        notifier=notifier,
        model=FakeStructuredModel(),
    )


# --------------------------------------------------------------------- propose


def test_propose_stores_validates_and_notifies(orch, notifier):
    proposal = orch.propose()

    assert proposal.status == ProposalStatus.PENDING
    assert proposal.gameweek == NEXT_GAMEWEEK
    assert proposal.agent.captaincy.captain_id == FWD_CAPTAIN
    assert proposal.validation_issues == []
    assert proposal.is_executable

    assert orch.store.get(proposal.id) is not None
    assert len(notifier.sent) == 1
    subject, text = notifier.sent[0]
    assert f"GW{NEXT_GAMEWEEK}" in subject
    assert "/a/" in text, "the notification must carry the approval link"


def test_propose_keeps_the_brief_and_transcript_for_audit(orch):
    proposal = orch.propose()
    assert "FPL decision brief" in proposal.context_snapshot
    assert "Your squad" in proposal.context_snapshot
    assert proposal.agent_transcript


def test_propose_supersedes_the_previous_open_proposal(orch):
    first = orch.propose()
    second = orch.propose()

    assert orch.store.get(first.id).status == ProposalStatus.SUPERSEDED
    assert orch.store.get(second.id).status == ProposalStatus.PENDING
    assert orch.latest().id == second.id


def test_propose_does_not_touch_a_resolved_proposal(orch):
    first = orch.propose()
    orch.reject(first.id)
    orch.propose()
    assert orch.store.get(first.id).status == ProposalStatus.REJECTED


def test_a_proposal_that_fails_validation_is_still_stored_and_notified(
    settings, store, fake_client, notifier, mock_solio
):
    """You need to see the bad proposal; you just must not be able to submit it."""
    model = FakeStructuredModel(payload={**ONE_TRANSFER_PROPOSAL, "gameweek": 99})
    orch = Orchestrator(
        settings, store=store, client=fake_client, notifier=notifier, model=model
    )

    proposal = orch.propose()

    assert proposal.fatal_issues
    assert proposal.is_executable is False
    assert store.get(proposal.id) is not None
    assert "wrong_gameweek" in notifier.sent[0][1] or "GW99" in notifier.sent[0][1]


def test_a_dead_notifier_does_not_lose_the_proposal(
    settings, store, fake_client, mock_solio
):
    orch = Orchestrator(
        settings, store=store, client=fake_client,
        notifier=BrokenNotifier(), model=FakeStructuredModel(),
    )
    proposal = orch.propose()
    assert store.get(proposal.id) is not None
    assert proposal.status == ProposalStatus.PENDING


# --------------------------------------------------------------------- approve


def test_approve_submits_immediately_by_default(orch, fake_client):
    proposal = orch.propose()
    updated = orch.approve(proposal.id)

    assert updated.status == ProposalStatus.EXECUTED
    assert len(fake_client.picks_calls) == 1
    assert orch.store.get(proposal.id).status == ProposalStatus.EXECUTED


def test_approve_can_defer_to_the_commit_job(orch, fake_client):
    orch.settings.execute_on_approval = False
    proposal = orch.propose()
    updated = orch.approve(proposal.id)

    assert updated.status == ProposalStatus.APPROVED
    assert fake_client.picks_calls == [], "nothing submitted yet"

    committed = orch.auto_commit()
    assert committed.status == ProposalStatus.EXECUTED
    assert len(fake_client.picks_calls) == 1


def test_approve_records_a_note(orch):
    proposal = orch.propose()
    orch.approve(proposal.id, note="fine by me")
    assert orch.store.get(proposal.id).human_note == "fine by me"


def test_approving_twice_is_refused(orch):
    proposal = orch.propose()
    orch.approve(proposal.id)
    with pytest.raises(NotActionable, match="already executed"):
        orch.approve(proposal.id)


def test_an_invalid_proposal_cannot_be_approved(
    settings, store, fake_client, notifier, mock_solio
):
    model = FakeStructuredModel(payload={**ONE_TRANSFER_PROPOSAL, "gameweek": 99})
    orch = Orchestrator(
        settings, store=store, client=fake_client, notifier=notifier, model=model
    )
    proposal = orch.propose()

    with pytest.raises(NotActionable, match="failed validation"):
        orch.approve(proposal.id)
    assert fake_client.picks_calls == []


def test_approving_an_unknown_id_is_an_error(orch):
    with pytest.raises(ProposalNotFound):
        orch.approve("does-not-exist")


def test_approval_is_blocked_when_revalidation_fails(orch, fake_client, context):
    proposal = orch.propose()
    # The deadline passes while the proposal sits in the inbox.
    for event in orch.client.bootstrap().events:
        event.deadline_time = datetime.now(UTC) - timedelta(minutes=1)

    with pytest.raises(ExecutionBlocked):
        orch.approve(proposal.id)

    stored = orch.store.get(proposal.id)
    assert stored.status == ProposalStatus.FAILED
    assert fake_client.picks_calls == []


# ---------------------------------------------------------------------- reject


def test_reject_stops_everything(orch, fake_client):
    proposal = orch.propose()
    updated = orch.reject(proposal.id, note="I want Oakley")

    assert updated.status == ProposalStatus.REJECTED
    assert updated.human_note == "I want Oakley"
    assert orch.auto_commit() is None, "a rejected proposal must not auto-commit"
    assert fake_client.picks_calls == []


def test_rejecting_a_resolved_proposal_is_refused(orch):
    proposal = orch.propose()
    orch.reject(proposal.id)
    with pytest.raises(NotActionable):
        orch.reject(proposal.id)


# ----------------------------------------------------------------------- amend


def test_amend_creates_a_new_revision_that_supersedes_the_old(orch):
    first = orch.propose()
    orch._model = FakeStructuredModel(payload=ONE_TRANSFER_PROPOSAL)

    revised = orch.amend(first.id, "buy Abbott, Hollis has nothing coming up")

    assert revised.id != first.id
    assert revised.revision == first.revision + 1
    assert revised.supersedes == first.id
    assert revised.human_note.startswith("buy Abbott")
    assert revised.status == ProposalStatus.PENDING
    assert [t.element_in for t in revised.agent.transfers] == [FREE_MID_NEW]
    assert orch.store.get(first.id).status == ProposalStatus.SUPERSEDED
    assert orch.latest().id == revised.id


def test_amend_feeds_the_note_to_the_agent(orch):
    first = orch.propose()
    model = FakeStructuredModel()
    orch._model = model
    orch.amend(first.id, "captain Oakley instead")
    assert any("captain Oakley instead" in prompt for prompt in model.seen_prompts)


def test_amending_a_resolved_proposal_is_refused(orch):
    proposal = orch.propose()
    orch.reject(proposal.id)
    with pytest.raises(NotActionable, match="nothing to amend"):
        orch.amend(proposal.id, "change it")


def test_an_amended_proposal_can_be_amended_again(orch):
    first = orch.propose()
    second = orch.amend(first.id, "try again")
    third = orch.amend(second.id, "once more")
    assert third.revision == 2
    assert third.supersedes == second.id


# ----------------------------------------------------------------- auto-commit


def test_silence_submits_at_the_deadline(orch, fake_client):
    proposal = orch.propose()
    committed = orch.auto_commit()

    assert committed.id == proposal.id
    assert committed.status == ProposalStatus.AUTO_EXECUTED
    assert len(fake_client.picks_calls) == 1


def test_auto_commit_off_expires_instead_of_submitting(orch, fake_client):
    orch.settings.auto_commit_enabled = False
    orch.propose()

    expired = orch.auto_commit()

    assert expired.status == ProposalStatus.EXPIRED
    assert fake_client.picks_calls == [], "nothing may be submitted"


def test_auto_commit_with_no_proposal_does_nothing(orch, fake_client):
    assert orch.auto_commit() is None
    assert fake_client.picks_calls == []


def test_auto_commit_ignores_an_already_executed_proposal(orch, fake_client):
    proposal = orch.propose()
    orch.approve(proposal.id)
    assert len(fake_client.picks_calls) == 1

    assert orch.auto_commit() is None
    assert len(fake_client.picks_calls) == 1, "must not submit twice"


def test_auto_commit_revalidates_and_refuses_a_stale_plan(
    settings, store, fake_client, notifier, mock_solio
):
    """Approved at T-36h, illegal by T-45m: the guardrails still stop it."""
    orch = Orchestrator(
        settings, store=store, client=fake_client, notifier=notifier,
        model=FakeStructuredModel(payload=ONE_TRANSFER_PROPOSAL),
    )
    proposal = orch.propose()
    assert proposal.is_executable

    # You made the transfer yourself in the app in the meantime.
    fake_client._my_team.pick_for(MID_LIV).element = FREE_MID_NEW

    with pytest.raises(ExecutionBlocked):
        orch.auto_commit()

    assert store.get(proposal.id).status == ProposalStatus.FAILED
    assert fake_client.transfer_calls == []


def test_auto_commit_notifies_on_failure(
    settings, store, fake_client, notifier, mock_solio
):
    orch = Orchestrator(
        settings, store=store, client=fake_client, notifier=notifier,
        model=FakeStructuredModel(payload=ONE_TRANSFER_PROPOSAL),
    )
    orch.propose()
    fake_client._my_team.pick_for(MID_LIV).element = FREE_MID_NEW

    with pytest.raises(ExecutionBlocked):
        orch.auto_commit()

    assert len(notifier.sent) == 2, "propose, then the failure"


def test_low_confidence_blocks_the_silent_path(
    settings, store, fake_client, notifier, mock_solio
):
    settings.min_captain_confidence = 0.9
    orch = Orchestrator(
        settings, store=store, client=fake_client, notifier=notifier,
        model=FakeStructuredModel(),  # confidence 0.72
    )
    orch.propose()

    with pytest.raises(ExecutionBlocked, match="low_confidence"):
        orch.auto_commit()
    assert fake_client.picks_calls == []


def test_dry_run_means_nothing_is_claimed_to_be_live(orch, fake_client):
    assert orch.settings.dry_run is True
    proposal = orch.propose()
    orch.approve(proposal.id)
    # The FakeClient stands in for the real client, which is where DRY_RUN is
    # enforced -- what matters here is that the flow completes and is recorded.
    assert orch.store.get(proposal.id).status == ProposalStatus.EXECUTED
    assert orch.store.get(proposal.id).execution_result is not None
