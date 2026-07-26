"""Notification rendering and channel selection.

The rendered message is the entire interface most of the time -- if the approval
link is missing or the auto-commit warning is wrong, you find out by losing a
gameweek. So: the link is always present, and what the message claims will happen
matches what the settings will actually do.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from fpl_buddy.approval import make_token, read_token
from fpl_buddy.decisions.schema import ProposalStatus
from fpl_buddy.decisions.validate import validate
from fpl_buddy.knowledge.harvest import HarvestReport
from fpl_buddy.knowledge.store import ArticleNote
from fpl_buddy.notify import (
    LogNotifier,
    Notifier,
    NullNotifier,
    SmtpNotifier,
    WebhookNotifier,
    build_notifier,
    render_harvest,
    render_proposal,
    safe_notify,
    safe_notify_harvest,
)

from .conftest import (
    FREE_MID_NEW,
    MID_LIV,
    make_proposal,
    make_stored,
    make_transfer,
)


def stored(context, **overrides):
    return make_stored(make_proposal(), context, **overrides)


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.meta: list[dict | None] = []

    def send(self, subject, text, *, html=None, meta=None) -> None:
        self.sent.append((subject, text))
        self.meta.append(meta)


# ------------------------------------------------------------------- rendering


def test_subject_names_the_gameweek_and_the_headline(context, settings):
    subject, _text, _html = render_proposal(stored(context), settings)
    assert subject.startswith("FPL GW4:")
    assert "(C) Vasquez" in subject


def test_body_states_the_decision(context, settings):
    _subject, text, _html = render_proposal(stored(context), settings)
    assert "Captain:" in text and "Vasquez" in text
    assert "Vice:" in text
    assert "Transfers:    none (rolling)" in text
    assert "Confidence:   70%" in text
    assert "Roll the transfer" in text


def test_body_shows_resolved_transfer_prices(context, settings):
    agent = make_proposal(
        transfers=[
            make_transfer(MID_LIV, FREE_MID_NEW, player_out_name="Hollis", player_in_name="Abbott")
        ]
    )
    validate(agent, context, settings)  # resolves the prices
    _subject, text, _html = render_proposal(make_stored(agent, context), settings)

    assert "Hollis -> Abbott" in text
    assert "sell £5.2m" in text
    assert "buy £5.0m" in text


def test_body_always_carries_the_approval_link(context, settings):
    proposal = stored(context)
    _subject, text, _html = render_proposal(proposal, settings)
    assert f"{settings.public_base_url}/a/" in text

    token = text.split("/a/")[1].strip().split()[0]
    assert read_token(settings, token) == proposal.id


def test_body_warns_that_silence_submits(context, settings):
    assert settings.auto_commit_enabled
    _subject, text, _html = render_proposal(stored(context), settings)
    assert "Doing nothing submits this automatically" in text
    assert "45 minutes" in text


def test_body_says_so_when_auto_commit_is_off(context, settings):
    settings.auto_commit_enabled = False
    _subject, text, _html = render_proposal(stored(context), settings)
    assert "Auto-commit is off" in text
    assert "Doing nothing submits" not in text


def test_body_flags_dry_run(context, settings):
    _subject, text, _html = render_proposal(stored(context), settings)
    assert "DRY_RUN is on" in text


def test_body_lists_validation_problems_and_refuses_to_promise_submission(context, settings):
    agent = make_proposal(gameweek=99)
    proposal = make_stored(
        agent, context, validation_issues=validate(agent, context, settings)
    )
    _subject, text, _html = render_proposal(proposal, settings)

    assert "[FATAL]" in text
    assert "will NOT be submitted" in text
    assert "Doing nothing submits" not in text


def test_warnings_are_shown_without_the_refusal_banner(context, settings):
    """A flagged captain is worth reading about; it isn't a blocker."""
    from .conftest import DEF_INJURED

    agent = make_proposal(
        captaincy=make_proposal().captaincy.model_copy(update={"captain_id": DEF_INJURED})
    )
    issues = validate(agent, context, settings)
    assert issues and not any(i.fatal for i in issues)

    _subject, text, _html = render_proposal(
        make_stored(agent, context, validation_issues=issues), settings
    )
    assert "[warn]" in text
    assert "will NOT be submitted" not in text


def test_risks_and_reasons_survive_into_the_message(context, settings):
    agent = make_proposal(risks=["Late fitness test on Friday"])
    _subject, text, _html = render_proposal(make_stored(agent, context), settings)
    assert "Risks:" in text
    assert "Late fitness test on Friday" in text
    assert "Armband:" in text


def test_html_alternative_is_produced(context, settings):
    _subject, _text, html = render_proposal(stored(context), settings)
    assert html.startswith("\n<!doctype html>") or html.lstrip().startswith("<!doctype html>")
    assert "Review this proposal" in html
    assert f"{settings.public_base_url}/a/" in html


def test_html_flags_a_failed_validation(context, settings):
    agent = make_proposal(gameweek=99)
    proposal = make_stored(agent, context, validation_issues=validate(agent, context, settings))
    _subject, _text, html = render_proposal(proposal, settings)
    assert "Failed validation" in html


# -------------------------------------------------------------------- channels


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        ("none", NullNotifier),
        ("log", LogNotifier),
        ("smtp", SmtpNotifier),
        ("webhook", WebhookNotifier),
    ],
)
def test_channel_selection(settings, channel, expected):
    settings.notify_channel = channel
    assert isinstance(build_notifier(settings), expected)


def test_smtp_without_configuration_is_an_explicit_error(settings, context):
    settings.notify_channel = "smtp"
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        build_notifier(settings).notify_proposal(stored(context), settings)


def test_webhook_without_a_url_is_an_explicit_error(settings, context):
    settings.notify_channel = "webhook"
    with pytest.raises(RuntimeError, match="WEBHOOK_URL"):
        build_notifier(settings).notify_proposal(stored(context), settings)


def test_webhook_posts_the_subject_text_and_proposal_id(settings, context):
    import httpx
    import respx

    settings.notify_channel = "webhook"
    settings.webhook_url = "https://hooks.example.test/fpl"
    proposal = stored(context)

    with respx.mock:
        route = respx.post(settings.webhook_url).mock(return_value=httpx.Response(200))
        build_notifier(settings).notify_proposal(proposal, settings)

    import json

    payload = json.loads(route.calls[0].request.content)
    assert payload["proposal_id"] == proposal.id
    assert "GW4" in payload["subject"]
    assert "/a/" in payload["text"]


def test_discord_without_a_bot_falls_back_to_posting_over_https(settings):
    """A cron job cannot hold a gateway connection, but it can still POST.

    Refusing to build a notifier here would mean the tick driver silently makes
    proposals nobody is told about.
    """
    from fpl_buddy.discord_bot.rest import DiscordRestNotifier

    settings.notify_channel = "discord"
    settings.discord_bot_token = SecretStr("token")
    settings.discord_channel_id = 42

    assert isinstance(build_notifier(settings), DiscordRestNotifier)


def test_discord_without_credentials_is_still_an_error(settings):
    """Falling back to HTTPS is not the same as inventing a channel to post to."""
    settings.notify_channel = "discord"
    settings.discord_bot_token = SecretStr("")
    settings.discord_channel_id = 0

    with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN"):
        build_notifier(settings)


def test_discord_with_a_bot_builds_a_discord_notifier(settings):
    from fpl_buddy.discord_bot.notifier import DiscordNotifier

    settings.notify_channel = "discord"
    notifier = build_notifier(settings, discord_bot=object())
    assert isinstance(notifier, DiscordNotifier)


def test_webhook_failure_is_raised(settings, context):
    import httpx
    import respx

    settings.notify_channel = "webhook"
    settings.webhook_url = "https://hooks.example.test/fpl"

    with respx.mock:
        respx.post(settings.webhook_url).mock(return_value=httpx.Response(500, text="nope"))
        with pytest.raises(RuntimeError, match="500"):
            build_notifier(settings).notify_proposal(stored(context), settings)


def test_safe_notify_swallows_failures(settings, context, caplog):
    """A dead notifier must never stop a proposal from existing."""

    class Broken(NullNotifier):
        def send(self, subject, text, *, html=None, meta=None):
            raise RuntimeError("smtp is on fire")

    safe_notify(Broken(), stored(context), settings)
    assert "Could not notify" in caplog.text
    assert "still auto-commit" in caplog.text


# ------------------------------------------------------------------- tokens


def test_tokens_are_scoped_to_one_proposal(settings):
    assert read_token(settings, make_token(settings, "abc")) == "abc"


def test_an_executed_proposal_does_not_claim_it_is_still_waiting(context, settings):
    """The post-submission message must not repeat the auto-commit promise."""
    proposal = stored(context, status=ProposalStatus.AUTO_EXECUTED)
    _subject, text, _html = render_proposal(proposal, settings)

    assert "Submitted to FPL" in text
    assert "Doing nothing submits" not in text


def test_a_failed_proposal_says_nothing_will_be_retried(context, settings):
    proposal = stored(
        context, status=ProposalStatus.FAILED, execution_error="FPL said no"
    )
    _subject, text, _html = render_proposal(proposal, settings)
    assert "Submission failed" in text
    assert "Doing nothing submits" not in text


def test_a_rejected_proposal_states_that_it_is_over(context, settings):
    proposal = stored(context, status=ProposalStatus.REJECTED)
    _subject, text, _html = render_proposal(proposal, settings)
    assert "Nothing further will happen" in text


# --------------------------------------------------------------- harvest summary
#
# The harvest runs daily and silently. Counts alone ("6 new from 41 candidates")
# tell you the machinery ran; they do not tell you whether it found the injury
# news you care about. These pin down that the message leads with the articles.


def note(**overrides) -> ArticleNote:
    defaults = dict(
        id="ffs-2026-07-25-abc",
        title="Five players to watch in GW1",
        url="https://example.com/gw1",
        source="fantasy-football-scout",
        summary="A long-winded overview of the opening weekend.",
        key_points=["Mosquera is nailed at left-back."],
    )
    return ArticleNote(**{**defaults, "id": overrides.pop("id", defaults["id"]), **overrides})


def test_the_summary_leads_with_the_articles_not_the_counters(settings):
    report = HarvestReport(considered=41, stored=1, notes=[note()])
    subject, text = render_harvest(report, settings)

    assert "1 new article" in subject
    assert text.index("Five players to watch") < text.index("41 candidate")
    assert "Mosquera is nailed at left-back." in text
    assert "https://example.com/gw1" in text


def test_a_quiet_day_says_so_plainly(settings):
    subject, text = render_harvest(HarvestReport(considered=12), settings)

    assert "0 new articles" in subject
    assert "Nothing new this time." in text


def test_videos_and_paywalled_articles_are_labelled(settings):
    report = HarvestReport(
        stored=2,
        notes=[
            note(id="a", kind="youtube", title="GW1 team reveal"),
            note(id="b", access="partial", title="Members-only preview"),
        ],
    )
    _subject, text = render_harvest(report, settings)

    assert "video" in text
    assert "[partial]" in text


def test_a_huge_harvest_is_capped_so_discord_accepts_it(settings):
    """Discord rejects anything over 2000 characters outright."""
    notes = [note(id=f"n{i}", title=f"Article number {i}") for i in range(40)]
    _subject, text = render_harvest(HarvestReport(stored=40, notes=notes), settings)

    assert "...and 28 more." in text
    assert len(text) < 2000


def test_failures_are_always_surfaced(settings):
    """A source that quietly broke looks exactly like a quiet news day."""
    report = HarvestReport(stored=0, failures=[f"source-{i} timed out" for i in range(8)])
    subject, text = render_harvest(report, settings)

    assert "8 failure(s)" in subject
    assert "source-0 timed out" in text
    assert "...and 3 more." in text


def test_the_summary_can_be_switched_off(settings):
    settings.notify_harvest = False
    notifier = RecordingNotifier()

    safe_notify_harvest(notifier, HarvestReport(stored=3, notes=[note()]), settings)

    assert notifier.sent == []


def test_a_dead_notifier_never_takes_the_harvest_down(settings):
    """Best-effort twice over: the harvest itself is optional enrichment."""
    class Broken(Notifier):
        def send(self, subject, text, *, html=None, meta=None):
            raise RuntimeError("discord is on fire")

    safe_notify_harvest(Broken(), HarvestReport(stored=1, notes=[note()]), settings)


def test_the_summary_is_tagged_so_a_webhook_can_route_it(settings):
    notifier = RecordingNotifier()
    safe_notify_harvest(notifier, HarvestReport(stored=1, notes=[note()]), settings)

    assert notifier.meta == [{"kind": "harvest"}]
