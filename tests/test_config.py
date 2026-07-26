"""Settings parsing.

Config is the one module every entrypoint touches before anything else, so a
parse error here is a crash at startup with no useful message. The blank-value
handling matters most: a half-filled `.env`, or a host injecting an unset secret
as an empty string, must fall back to defaults rather than explode -- and must
fall back in the *safe* direction.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fpl_buddy.config import Settings


def build(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ------------------------------------------------------------ blank handling


def test_blank_int_falls_back_to_the_default():
    """`FPL_ENTRY_ID=` in a template must not be a startup crash."""
    assert build(fpl_entry_id="").fpl_entry_id == 0


def test_blank_float_falls_back_to_the_default():
    assert build(propose_hours_before_deadline="").propose_hours_before_deadline == 1.0


def test_blank_bool_falls_back_to_the_safe_default():
    """A blank DRY_RUN has to mean on, never off."""
    assert build(dry_run="").dry_run is True


def test_blank_auto_commit_stays_enabled_by_default():
    assert build(auto_commit_enabled="").auto_commit_enabled is True


def test_blank_literal_falls_back_to_the_default():
    assert build(state_backend="").state_backend == "file"
    assert build(notify_channel="").notify_channel == "log"


def test_whitespace_only_is_treated_as_blank():
    assert build(fpl_entry_id="   ").fpl_entry_id == 0
    assert build(max_points_hit="\t").max_points_hit == 0


def test_blank_string_with_a_non_empty_default_falls_back():
    assert build(azure_openai_deployment="").azure_openai_deployment == "gpt-4.1"
    assert build(timezone="").timezone == "Asia/Kolkata"


def test_blank_secret_stays_empty_and_reads_as_unconfigured():
    settings = build(fpl_cookie_header="", azure_openai_api_key="")
    assert settings.fpl_cookie_header.get_secret_value() == ""
    assert settings.has_cookie_header is False


def test_real_values_still_parse():
    settings = build(
        fpl_entry_id="1234567",
        dry_run="false",
        max_points_hit="4",
        propose_hours_before_deadline="12.5",
        notify_channel="webhook",
    )
    assert settings.fpl_entry_id == 1234567
    assert settings.dry_run is False
    assert settings.max_points_hit == 4
    assert settings.propose_hours_before_deadline == 12.5
    assert settings.notify_channel == "webhook"


def test_a_genuinely_invalid_value_is_still_rejected():
    """Blank is forgiven; wrong is not."""
    with pytest.raises(ValidationError):
        build(fpl_entry_id="not-a-number")
    with pytest.raises(ValidationError):
        build(notify_channel="carrier-pigeon")


# ------------------------------------------------------------------ defaults


def test_the_dangerous_switches_default_safely():
    settings = build()
    assert settings.dry_run is True, "no POST may leave the process by default"
    assert settings.max_points_hit == 0, "never take a hit unless asked"
    assert settings.min_captain_confidence == 0.0
    assert settings.state_backend == "file"


def test_credential_helpers_read_correctly():
    assert build().has_login_credentials is False
    assert build(fpl_email="a@b.test", fpl_password="pw").has_login_credentials is True
    assert build(fpl_email="a@b.test").has_login_credentials is False, "needs both"
    assert build(fpl_cookie_header="pl_profile=x; sessionid=y").has_cookie_header is True


def test_discord_needs_both_the_token_and_the_channel():
    assert build().has_discord is False
    assert build(discord_bot_token="abc").has_discord is False, "no channel to post to"
    assert build(discord_channel_id="123").has_discord is False, "no token to connect with"
    assert build(discord_bot_token="abc", discord_channel_id="123").has_discord is True


def test_trailing_slash_is_stripped_from_the_public_url():
    assert build(public_base_url="https://x.test/").public_base_url == "https://x.test"
    assert build(public_base_url="https://x.test///").public_base_url == "https://x.test"


def test_unknown_env_vars_are_ignored():
    """The process environment is full of things that aren't ours."""
    assert build(SOME_UNRELATED_THING="x").fpl_entry_id == 0


def test_secrets_do_not_leak_into_the_repr():
    settings = build(fpl_password="hunter2", azure_openai_api_key="sk-secret")
    text = repr(settings)
    assert "hunter2" not in text
    assert "sk-secret" not in text


# --------------------------------------------------------- discord channels


def test_harvest_goes_to_its_own_channel_when_one_is_set():
    s = build(discord_channel_id="123", discord_harvest_channel_id="456")
    assert s.discord_channel_for("harvest") == 456
    assert s.discord_channel_for("") == 123


def test_an_unset_harvest_channel_shares_the_main_one():
    """Adding the setting must not change behaviour for anyone who ignores it."""
    s = build(discord_channel_id="123")
    assert s.discord_channel_for("harvest") == 123


def test_an_unknown_kind_lands_in_the_main_channel():
    """Wrong channel is a nuisance; nowhere at all is a lost notification."""
    s = build(discord_channel_id="123", discord_harvest_channel_id="456")
    assert s.discord_channel_for("something-new") == 123


def test_a_harvest_channel_alone_does_not_make_discord_configured():
    """has_discord still means "can I post proposals", which needs the main one."""
    assert build(discord_harvest_channel_id="456").has_discord is False
