"""Proposal persistence.

The store is what makes "wait for the human, then commit at the deadline"
survive a container restart. If a pending proposal can't be found again 36 hours
later, the whole design collapses into "sometimes nothing happens".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_buddy.decisions.schema import Proposal, ProposalStatus
from fpl_buddy.decisions.store import FileProposalStore, build_store

from .conftest import make_proposal, make_stored


@pytest.fixture
def store(tmp_path: Path) -> FileProposalStore:
    return FileProposalStore(tmp_path / "proposals")


def stored(context, proposal_id: str, **overrides) -> Proposal:
    return make_stored(make_proposal(), context, id=proposal_id, **overrides)


def test_save_and_get_round_trip(store, context):
    proposal = stored(context, "p1")
    store.save(proposal)

    loaded = store.get("p1")
    assert loaded is not None
    assert loaded.id == "p1"
    assert loaded.agent.captaincy.captain_id == proposal.agent.captaincy.captain_id
    assert loaded.status == ProposalStatus.PENDING


def test_get_unknown_id_is_none(store):
    assert store.get("nope") is None


def test_saving_twice_updates_in_place(store, context):
    proposal = stored(context, "p1")
    store.save(proposal)
    proposal.touch(ProposalStatus.APPROVED)
    store.save(proposal)

    assert store.get("p1").status == ProposalStatus.APPROVED
    assert len(store.list_all()) == 1


def test_save_is_atomic_leaving_no_temp_files(store, context):
    store.save(stored(context, "p1"))
    assert list(store.directory.glob("*.tmp")) == []
    assert [p.name for p in store.directory.glob("*.json")] == ["p1.json"]


def test_latest_prefers_the_newest(store, context):
    old = stored(context, "old", created_at=datetime.now(UTC) - timedelta(hours=5))
    new = stored(context, "new", created_at=datetime.now(UTC))
    store.save(old)
    store.save(new)
    assert store.latest().id == "new"


def test_latest_can_be_scoped_to_a_gameweek(store, context):
    store.save(stored(context, "gw4", gameweek=4))
    store.save(stored(context, "gw5", gameweek=5, created_at=datetime.now(UTC) + timedelta(hours=1)))
    assert store.latest(gameweek=4).id == "gw4"
    assert store.latest().id == "gw5"


def test_latest_breaks_ties_on_revision(store, context):
    when = datetime.now(UTC)
    store.save(stored(context, "r0", created_at=when, revision=0))
    store.save(stored(context, "r1", created_at=when, revision=1))
    assert store.latest().id == "r1"


def test_latest_is_none_when_empty(store):
    assert store.latest() is None


def test_pending_lists_only_untouched_proposals(store, context):
    store.save(stored(context, "a"))
    store.save(stored(context, "b", status=ProposalStatus.REJECTED))
    store.save(stored(context, "c", status=ProposalStatus.EXECUTED))
    assert {p.id for p in store.pending()} == {"a"}


def test_superseding_marks_older_open_proposals(store, context):
    store.save(stored(context, "old-pending"))
    store.save(stored(context, "old-amended", status=ProposalStatus.AMENDED))
    store.save(stored(context, "old-rejected", status=ProposalStatus.REJECTED))
    store.save(stored(context, "current"))

    count = store.supersede_open_proposals(4, except_id="current")

    assert count == 2
    assert store.get("old-pending").status == ProposalStatus.SUPERSEDED
    assert store.get("old-amended").status == ProposalStatus.SUPERSEDED
    assert store.get("old-rejected").status == ProposalStatus.REJECTED, "already resolved"
    assert store.get("current").status == ProposalStatus.PENDING


def test_superseding_leaves_other_gameweeks_alone(store, context):
    store.save(stored(context, "gw5", gameweek=5))
    assert store.supersede_open_proposals(4) == 0
    assert store.get("gw5").status == ProposalStatus.PENDING


def test_a_corrupt_file_is_skipped_not_fatal(store, context):
    store.save(stored(context, "good"))
    (store.directory / "broken.json").write_text("{ not json")
    assert {p.id for p in store.list_all()} == {"good"}
    assert store.get("broken") is None


def test_directory_is_created_on_demand(tmp_path: Path):
    store = FileProposalStore(tmp_path / "deep" / "nested" / "proposals")
    assert store.directory.is_dir()


def test_validation_issues_and_audit_trail_survive_a_round_trip(store, context, settings):
    from fpl_buddy.decisions.validate import validate

    agent = make_proposal(gameweek=99)  # guaranteed to produce an issue
    proposal = make_stored(
        agent,
        context,
        id="p1",
        validation_issues=validate(agent, context, settings),
        context_snapshot=context.render(),
        agent_transcript="[ai] thinking out loud",
        human_note="captain someone else",
    )
    store.save(proposal)

    loaded = store.get("p1")
    assert [i.code for i in loaded.validation_issues] == ["wrong_gameweek"]
    assert loaded.fatal_issues
    assert loaded.is_executable is False
    assert "FPL decision brief" in loaded.context_snapshot
    assert loaded.agent_transcript.startswith("[ai]")
    assert loaded.human_note == "captain someone else"


def test_execution_result_survives_a_round_trip(store, context):
    proposal = stored(
        context,
        "p1",
        status=ProposalStatus.FAILED,
        execution_result={"transfers": {"dry_run": True}},
        execution_error="Transfers applied but picks failed: boom",
    )
    store.save(proposal)
    loaded = store.get("p1")
    assert loaded.execution_result == {"transfers": {"dry_run": True}}
    assert "picks failed" in loaded.execution_error


# ------------------------------------------------------------------- selection


def test_build_store_defaults_to_files(settings):
    store = build_store(settings)
    assert isinstance(store, FileProposalStore)
    assert store.directory == Path(settings.state_dir) / "proposals"


def test_azure_table_backend_needs_a_connection_string(settings):
    settings.state_backend = "azure_table"
    with pytest.raises(RuntimeError, match="AZURE_TABLE_CONNECTION_STRING"):
        build_store(settings)


# --------------------------------------------------------------- state machine


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        (ProposalStatus.PENDING, False),
        (ProposalStatus.APPROVED, False),
        (ProposalStatus.AMENDED, False),
        (ProposalStatus.FAILED, False),
        (ProposalStatus.REJECTED, True),
        (ProposalStatus.EXECUTED, True),
        (ProposalStatus.AUTO_EXECUTED, True),
        (ProposalStatus.EXPIRED, True),
        (ProposalStatus.SUPERSEDED, True),
    ],
)
def test_terminal_statuses(context, status, terminal):
    assert make_stored(make_proposal(), context, status=status).is_terminal is terminal


def test_touch_updates_the_timestamp(context):
    proposal = make_stored(make_proposal(), context)
    before = proposal.updated_at
    proposal.touch(ProposalStatus.APPROVED)
    assert proposal.status == ProposalStatus.APPROVED
    assert proposal.updated_at >= before


def test_headline_reads_like_a_notification(context):
    from .conftest import FREE_MID_NEW, MID_LIV, make_transfer

    agent = make_proposal(
        transfers=[
            make_transfer(MID_LIV, FREE_MID_NEW, player_out_name="Hollis", player_in_name="Abbott")
        ],
        points_hit=4,
        chip="3xc",
    )
    headline = make_stored(agent, context).headline()
    assert "(C) Vasquez" in headline
    assert "Hollis -> Abbott" in headline
    assert "-4 hit" in headline
    assert "chip: 3xc" in headline


def test_headline_says_rolling_when_there_are_no_transfers(context):
    assert "no transfers (rolling)" in make_stored(make_proposal(), context).headline()
