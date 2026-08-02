"""The HTTP surface, with emphasis on the approval link.

The link is the credential, so the tests that matter are the ones about what a
link can and cannot do: it must not act on a GET (link scanners prefetch), it
must not work for a different proposal, and it must expire.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fpl_buddy.api import create_app
from fpl_buddy.approval import make_token
from fpl_buddy.decisions.schema import ProposalStatus
from fpl_buddy.decisions.store import FileProposalStore
from fpl_buddy.orchestrator import Orchestrator

from .conftest import NEXT_GAMEWEEK
from .fakes import ONE_TRANSFER_PROPOSAL, FakeStructuredModel


@pytest.fixture
def store(tmp_path: Path) -> FileProposalStore:
    return FileProposalStore(tmp_path / "proposals")


@pytest.fixture
def orch(settings, store, fake_client, mock_solio) -> Orchestrator:
    from fpl_buddy.notify import NullNotifier

    return Orchestrator(
        settings, store=store, client=fake_client,
        notifier=NullNotifier(), model=FakeStructuredModel(),
    )


@pytest.fixture
def client(settings, orch) -> TestClient:
    return TestClient(create_app(settings, orchestrator=orch))


@pytest.fixture
def proposal(orch):
    return orch.propose()


def token_for(settings, proposal_id: str) -> str:
    return make_token(settings, proposal_id)


# ---------------------------------------------------------------------- health


def test_health_reports_the_safety_switches(client, settings):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["dry_run"] is True
    assert body["auto_commit"] is settings.auto_commit_enabled
    assert body["entry_id"] == settings.fpl_entry_id


def test_healthz_stays_as_an_alias(client):
    """Deployments and the Docker healthcheck moved to /health because Google's
    frontend swallows /healthz on *.run.app, but the old path still answers."""
    assert client.get("/healthz").json() == client.get("/health").json()


# ----------------------------------------------------------------------- reads


def test_latest_returns_the_proposal(client, proposal):
    body = client.get("/proposals/latest").json()
    assert body["id"] == proposal.id
    assert body["gameweek"] == NEXT_GAMEWEEK
    assert body["headline"]
    assert body["is_executable"] is True
    assert body["review_url"].startswith("https://fpl.example.test/a/")


def test_latest_is_404_before_anything_exists(client):
    assert client.get("/proposals/latest").status_code == 404


def test_reads_omit_the_bulky_audit_fields(client, proposal):
    body = client.get(f"/proposals/{proposal.id}").json()
    assert "context_snapshot" not in body
    assert "agent_transcript" not in body


def test_unknown_proposal_is_404(client, proposal):
    assert client.get("/proposals/nope").status_code == 404


def test_api_key_gates_reads_when_set(settings, orch, proposal):
    from pydantic import SecretStr

    settings.api_key = SecretStr("let-me-in")
    guarded = TestClient(create_app(settings, orchestrator=orch))

    assert guarded.get("/proposals/latest").status_code == 401
    assert guarded.get("/proposals/latest", headers={"X-API-Key": "wrong"}).status_code == 401
    assert guarded.get("/proposals/latest", headers={"X-API-Key": "let-me-in"}).status_code == 200
    assert guarded.get("/health").status_code == 200, "health stays open for probes"


# -------------------------------------------------------------- the review page


def test_review_page_renders_the_proposal(client, settings, proposal):
    response = client.get(f"/a/{token_for(settings, proposal.id)}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert f"Gameweek {NEXT_GAMEWEEK}" in body
    assert "Approve" in body and "Reject" in body and "Amend" in body


def test_following_the_link_changes_nothing(client, settings, proposal, orch, fake_client):
    """Mail clients and scanners prefetch links; a GET must never act."""
    client.get(f"/a/{token_for(settings, proposal.id)}")
    client.get(f"/a/{token_for(settings, proposal.id)}")

    assert orch.store.get(proposal.id).status == ProposalStatus.PENDING
    assert fake_client.picks_calls == []
    assert fake_client.transfer_calls == []


def test_review_page_hides_the_buttons_once_resolved(client, settings, proposal, orch):
    orch.reject(proposal.id)
    body = client.get(f"/a/{token_for(settings, proposal.id)}").text
    assert "Approve" not in body
    assert "rejected" in body


def test_review_page_flags_a_failed_validation(
    settings, store, fake_client, mock_solio
):
    from fpl_buddy.notify import NullNotifier

    orch = Orchestrator(
        settings, store=store, client=fake_client, notifier=NullNotifier(),
        model=FakeStructuredModel(payload={**ONE_TRANSFER_PROPOSAL, "gameweek": 99}),
    )
    bad = orch.propose()
    client = TestClient(create_app(settings, orchestrator=orch))

    body = client.get(f"/a/{token_for(settings, bad.id)}").text
    assert "Failed validation" in body
    assert "Approve" not in body, "an invalid proposal must not be approvable"


def test_dry_run_is_stated_on_the_page(client, settings, proposal):
    body = client.get(f"/a/{token_for(settings, proposal.id)}").text
    assert "DRY_RUN is on" in body


# ------------------------------------------------------------------- acting


def test_posting_approve_submits(client, settings, proposal, orch, fake_client):
    response = client.post(
        f"/a/{token_for(settings, proposal.id)}", data={"action": "approve", "note": ""}
    )
    assert response.status_code == 200
    assert orch.store.get(proposal.id).status == ProposalStatus.EXECUTED
    assert len(fake_client.picks_calls) == 1
    assert "Executed" in response.text


def test_posting_reject_stops_everything(client, settings, proposal, orch, fake_client):
    response = client.post(
        f"/a/{token_for(settings, proposal.id)}", data={"action": "reject", "note": "no thanks"}
    )
    assert response.status_code == 200
    assert orch.store.get(proposal.id).status == ProposalStatus.REJECTED
    assert orch.store.get(proposal.id).human_note == "no thanks"
    assert fake_client.picks_calls == []


def test_amend_needs_a_note(client, settings, proposal, orch):
    response = client.post(
        f"/a/{token_for(settings, proposal.id)}", data={"action": "amend", "note": "   "}
    )
    assert response.status_code == 409
    assert orch.store.get(proposal.id).status == ProposalStatus.PENDING


def test_amend_with_a_note_produces_a_new_revision(client, settings, proposal, orch):
    response = client.post(
        f"/a/{token_for(settings, proposal.id)}",
        data={"action": "amend", "note": "captain Oakley"},
    )
    assert response.status_code == 200
    assert orch.store.get(proposal.id).status == ProposalStatus.SUPERSEDED
    assert orch.latest().id != proposal.id


def test_unknown_action_is_rejected(client, settings, proposal):
    response = client.post(
        f"/a/{token_for(settings, proposal.id)}", data={"action": "delete", "note": ""}
    )
    assert response.status_code == 409


def test_acting_twice_conflicts(client, settings, proposal):
    url = f"/a/{token_for(settings, proposal.id)}"
    assert client.post(url, data={"action": "approve", "note": ""}).status_code == 200
    assert client.post(url, data={"action": "approve", "note": ""}).status_code == 409


# ----------------------------------------------------------------- token rules


def test_a_garbage_token_is_403(client):
    assert client.get("/a/not-a-real-token").status_code == 403


def test_a_token_signed_with_another_secret_is_403(settings, orch, proposal):
    from pydantic import SecretStr

    other = settings.model_copy(update={"approval_secret": SecretStr("different-secret")})
    forged = make_token(other, proposal.id)
    client = TestClient(create_app(settings, orchestrator=orch))
    assert client.get(f"/a/{forged}").status_code == 403


def test_rotating_the_secret_invalidates_outstanding_links(settings, orch, proposal):
    from pydantic import SecretStr

    old_token = make_token(settings, proposal.id)
    settings.approval_secret = SecretStr("rotated")
    client = TestClient(create_app(settings, orchestrator=orch))
    assert client.get(f"/a/{old_token}").status_code == 403


def test_an_expired_token_is_403(settings, orch, proposal, monkeypatch):
    settings.approval_link_ttl_hours = 1
    token = make_token(settings, proposal.id)
    client = TestClient(create_app(settings, orchestrator=orch))
    assert client.get(f"/a/{token}").status_code == 200

    # Two days later, the same link is dead. itsdangerous reads the clock as
    # `time.time()`, so stand in a shim for the module it holds.
    import time as real_time
    from types import SimpleNamespace

    import itsdangerous.timed

    monkeypatch.setattr(
        itsdangerous.timed,
        "time",
        SimpleNamespace(time=lambda: real_time.time() + 2 * 86400),
    )
    assert client.get(f"/a/{token}").status_code == 403


def test_a_zero_ttl_is_rejected_by_config():
    """0 reads as 'expire now' but would mean 'no age check'. Refuse it."""
    from pydantic import ValidationError

    from fpl_buddy.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, approval_link_ttl_hours=0)


def test_a_token_is_scoped_to_one_proposal(client, settings, proposal, orch):
    second = orch.propose()
    wrong_token = make_token(settings, proposal.id)

    response = client.post(f"/proposals/{second.id}/approve", params={"token": wrong_token})
    assert response.status_code == 403
    assert orch.store.get(second.id).status == ProposalStatus.PENDING


def test_a_token_for_a_deleted_proposal_is_404(client, settings):
    assert client.get(f"/a/{make_token(settings, 'gone')}").status_code == 404


# --------------------------------------------------------- JSON action endpoints


def test_json_approve_needs_a_token(client, proposal):
    assert client.post(f"/proposals/{proposal.id}/approve").status_code == 422


def test_json_approve_works_with_a_matching_token(client, settings, proposal, orch):
    response = client.post(
        f"/proposals/{proposal.id}/approve",
        params={"token": token_for(settings, proposal.id)},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    assert orch.store.get(proposal.id).status == ProposalStatus.EXECUTED


def test_json_reject_records_the_note(client, settings, proposal, orch):
    response = client.post(
        f"/proposals/{proposal.id}/reject",
        params={"token": token_for(settings, proposal.id), "note": "not this week"},
    )
    assert response.status_code == 200
    assert orch.store.get(proposal.id).human_note == "not this week"


def test_json_amend_requires_a_non_empty_note(client, settings, proposal):
    response = client.post(
        f"/proposals/{proposal.id}/amend",
        params={"token": token_for(settings, proposal.id), "note": "  "},
    )
    assert response.status_code == 422


def test_json_endpoints_report_conflicts(client, settings, proposal, orch):
    orch.reject(proposal.id)
    response = client.post(
        f"/proposals/{proposal.id}/approve",
        params={"token": token_for(settings, proposal.id)},
    )
    assert response.status_code == 409
