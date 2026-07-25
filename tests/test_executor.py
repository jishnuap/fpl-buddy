"""The executor: re-validate, then submit -- and refuse when anything smells.

The invariant under test is that nothing is POSTed until the proposal has been
re-checked against a freshly built context, and that a partial failure is
recorded honestly rather than reported as success.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl_buddy.decisions.executor import ExecutionBlocked, execute
from fpl_buddy.decisions.schema import ProposalStatus
from fpl_buddy.fpl.client import TransferRejected

from .conftest import (
    FREE_MID_NEW,
    FWD_CAPTAIN,
    GK_RESERVE,
    MID_LIV,
    MID_VICE,
    FakeClient,
    make_proposal,
    make_stored,
    make_transfer,
)


def test_clean_proposal_submits_picks_only(context, settings, fake_client):
    proposal = make_stored(make_proposal(), context)

    execute(proposal, settings, fake_client, final_status=ProposalStatus.EXECUTED, context=context)

    assert proposal.status == ProposalStatus.EXECUTED
    assert proposal.execution_error is None
    assert fake_client.transfer_calls == [], "rolling means no transfer call at all"
    assert len(fake_client.picks_calls) == 1
    assert len(fake_client.picks_calls[0]["picks"]) == 15


def test_transfers_go_before_picks(context, settings, fake_client):
    """The squad has to exist before you can captain inside it."""
    agent = make_proposal(transfers=[make_transfer(MID_LIV, FREE_MID_NEW)])
    from fpl_buddy.decisions.validate import validate

    validate(agent, context, settings)  # resolves prices, as the propose flow does
    proposal = make_stored(agent, context)

    execute(proposal, settings, fake_client, final_status=ProposalStatus.EXECUTED, context=context)

    assert len(fake_client.transfer_calls) == 1
    assert len(fake_client.picks_calls) == 1
    call = fake_client.transfer_calls[0]
    assert call["event"] == context.gameweek.id
    assert call["transfers"] == [
        {
            "element_in": FREE_MID_NEW,
            "element_out": MID_LIV,
            "purchase_price": context.bootstrap.player(FREE_MID_NEW).now_cost,
            "selling_price": context.my_team.pick_for(MID_LIV).selling_price,
        }
    ]


def test_picks_payload_carries_the_armband_and_the_reserve_keeper(context, settings, fake_client):
    proposal = make_stored(make_proposal(), context)
    execute(proposal, settings, fake_client, final_status=ProposalStatus.EXECUTED, context=context)

    picks = fake_client.picks_calls[0]["picks"]
    assert [p["element"] for p in picks if p["is_captain"]] == [FWD_CAPTAIN]
    assert [p["element"] for p in picks if p["is_vice_captain"]] == [MID_VICE]
    assert picks[11]["element"] == GK_RESERVE
    assert [p["position"] for p in picks] == list(range(1, 16))


def test_stale_prices_are_re_resolved_at_execution_time(context, settings, fake_client):
    """Prices move between proposing and committing, so re-validation fixes them.

    A proposal stored 36 hours ago carries 36-hour-old prices. Submitting those
    gets the request rejected at best, and buys at the wrong valuation at worst.
    """
    agent = make_proposal(
        transfers=[make_transfer(MID_LIV, FREE_MID_NEW, selling_price=999, purchase_price=1)]
    )
    proposal = make_stored(agent, context)

    execute(proposal, settings, fake_client, final_status=ProposalStatus.EXECUTED, context=context)

    sent = fake_client.transfer_calls[0]["transfers"][0]
    assert sent["selling_price"] == context.my_team.pick_for(MID_LIV).selling_price
    assert sent["purchase_price"] == context.bootstrap.player(FREE_MID_NEW).now_cost
    assert sent["selling_price"] != 999 and sent["purchase_price"] != 1


def test_a_transfer_with_no_prices_at_all_never_reaches_the_wire(context, settings, fake_client):
    """Belt and braces: to_payload() refuses rather than sending nulls.

    Re-validation normally fills these in, so this guards the path where
    validation is bypassed or a future refactor reorders the two steps.
    """
    move = make_transfer(MID_LIV, FREE_MID_NEW)
    assert move.selling_price is None
    with pytest.raises(ValueError, match="prices were never resolved"):
        move.to_payload()


# ------------------------------------------------------------------- refusals


def test_a_fatal_issue_blocks_submission(context, settings, fake_client):
    proposal = make_stored(make_proposal(gameweek=99), context)

    with pytest.raises(ExecutionBlocked) as excinfo:
        execute(
            proposal, settings, fake_client,
            final_status=ProposalStatus.AUTO_EXECUTED, context=context,
        )

    assert "wrong_gameweek" in str(excinfo.value)
    assert proposal.status == ProposalStatus.FAILED
    assert proposal.execution_error
    assert fake_client.transfer_calls == [] and fake_client.picks_calls == []


def test_the_deadline_cutoff_blocks_submission(context, settings, fake_client):
    context.gameweek.deadline_time = datetime.now(UTC) + timedelta(seconds=30)
    proposal = make_stored(make_proposal(), context)

    with pytest.raises(ExecutionBlocked, match="past_deadline"):
        execute(
            proposal, settings, fake_client,
            final_status=ProposalStatus.AUTO_EXECUTED, context=context,
        )
    assert fake_client.picks_calls == []


def test_a_squad_that_changed_underneath_us_blocks_submission(context, settings, fake_client):
    """You made the transfer yourself in the app; the stored plan is now illegal."""
    agent = make_proposal(transfers=[make_transfer(MID_LIV, FREE_MID_NEW)])
    from fpl_buddy.decisions.validate import validate

    validate(agent, context, settings)
    proposal = make_stored(agent, context)

    # Simulate the human having already done it: MID_LIV is gone from the squad.
    pick = context.my_team.pick_for(MID_LIV)
    pick.element = FREE_MID_NEW

    with pytest.raises(ExecutionBlocked) as excinfo:
        execute(
            proposal, settings, fake_client,
            final_status=ProposalStatus.AUTO_EXECUTED, context=context,
        )
    assert "not_in_squad" in str(excinfo.value) or "already_owned" in str(excinfo.value)
    assert fake_client.transfer_calls == []


def test_low_confidence_blocks_when_a_threshold_is_set(context, settings, fake_client):
    settings.min_captain_confidence = 0.9
    proposal = make_stored(make_proposal(confidence=0.4), context)

    with pytest.raises(ExecutionBlocked, match="low_confidence"):
        execute(
            proposal, settings, fake_client,
            final_status=ProposalStatus.AUTO_EXECUTED, context=context,
        )
    assert proposal.status == ProposalStatus.FAILED
    assert fake_client.picks_calls == []


def test_confidence_above_the_threshold_proceeds(context, settings, fake_client):
    settings.min_captain_confidence = 0.5
    proposal = make_stored(make_proposal(confidence=0.8), context)
    execute(proposal, settings, fake_client, final_status=ProposalStatus.EXECUTED, context=context)
    assert proposal.status == ProposalStatus.EXECUTED


def test_zero_threshold_never_blocks(context, settings, fake_client):
    assert settings.min_captain_confidence == 0.0
    proposal = make_stored(make_proposal(confidence=0.0), context)
    execute(proposal, settings, fake_client, final_status=ProposalStatus.EXECUTED, context=context)
    assert proposal.status == ProposalStatus.EXECUTED


# -------------------------------------------------------------- partial failure


def test_a_rejected_transfer_stops_before_picks(context, settings, bootstrap, my_team, fixtures_list):
    client = FakeClient(
        bootstrap, my_team, fixtures_list,
        transfers_error=TransferRejected("Not enough money", status_code=400),
    )
    from fpl_buddy.decisions.validate import validate

    agent = make_proposal(transfers=[make_transfer(MID_LIV, FREE_MID_NEW)])
    validate(agent, context, settings)
    proposal = make_stored(agent, context)

    with pytest.raises(TransferRejected):
        execute(proposal, settings, client, final_status=ProposalStatus.EXECUTED, context=context)

    assert proposal.status == ProposalStatus.FAILED
    assert "Not enough money" in proposal.execution_error
    assert client.picks_calls == [], "never set the captain on a squad that didn't change"


def test_transfers_applied_but_picks_failed_is_recorded_honestly(
    context, settings, bootstrap, my_team, fixtures_list
):
    client = FakeClient(
        bootstrap, my_team, fixtures_list,
        picks_error=TransferRejected("Invalid picks", status_code=400),
    )
    from fpl_buddy.decisions.validate import validate

    agent = make_proposal(transfers=[make_transfer(MID_LIV, FREE_MID_NEW)])
    validate(agent, context, settings)
    proposal = make_stored(agent, context)

    with pytest.raises(TransferRejected):
        execute(proposal, settings, client, final_status=ProposalStatus.EXECUTED, context=context)

    assert proposal.status == ProposalStatus.FAILED
    assert "Transfers applied but picks failed" in proposal.execution_error
    assert proposal.execution_result["transfers"] == {"submitted": 1}
    assert len(client.transfer_calls) == 1


# ------------------------------------------------------------------ chip routing


@pytest.mark.parametrize("chip", ["wildcard", "freehit"])
def test_transfer_chips_go_on_the_transfers_call(context, settings, fake_client, chip):
    from fpl_buddy.decisions.validate import validate

    agent = make_proposal(chip=chip, transfers=[make_transfer(MID_LIV, FREE_MID_NEW)])
    context.my_team.chips_available = [chip]
    validate(agent, context, settings)
    proposal = make_stored(agent, context)

    execute(proposal, settings, fake_client, final_status=ProposalStatus.EXECUTED, context=context)

    assert fake_client.transfer_calls[0]["chip"] == chip
    assert fake_client.picks_calls[0]["chip"] is None


@pytest.mark.parametrize("chip", ["bboost", "3xc"])
def test_team_chips_go_on_the_picks_call(context, settings, fake_client, chip):
    context.my_team.chips_available = [chip]
    proposal = make_stored(make_proposal(chip=chip), context)

    execute(proposal, settings, fake_client, final_status=ProposalStatus.EXECUTED, context=context)

    assert fake_client.picks_calls[0]["chip"] == chip
    assert fake_client.transfer_calls == []


# --------------------------------------------------------------- fresh context


def test_executor_rebuilds_the_context_when_not_given_one(
    context, settings, fake_client, mock_solio
):
    """Never trust the stored snapshot -- it can be a day and a half old."""
    proposal = make_stored(make_proposal(), context)

    execute(proposal, settings, fake_client, final_status=ProposalStatus.AUTO_EXECUTED)

    assert fake_client.bootstrap_calls >= 1, "must have refetched bootstrap-static"
    assert proposal.status == ProposalStatus.AUTO_EXECUTED
