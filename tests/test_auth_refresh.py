"""OAuth token refresh.

This is the code that keeps the deadline job alive. The service proposes at
T-36h and commits at T-45m, while the access token lives 8 hours -- so the token
in hand when a proposal is made is always dead by the time it is submitted. If
refresh doesn't work, the product doesn't work, and it fails at the single worst
moment available.

The rotation tests matter just as much: PingOne issues a new refresh token on
every use, so failing to persist the response leaves you presenting a spent
token on the next attempt.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from fpl_buddy.fpl.auth import (
    DEFAULT_CLIENT_ID,
    FPLAuthenticator,
    FPLAuthError,
    FPLTokenRefreshError,
    SessionCookies,
    decode_jwt_claims,
)
from fpl_buddy.fpl.client import FPLClient

ISSUER = "https://auth.pingone.eu/test-env/as"
TOKEN_URL = f"{ISSUER}/token"
CLIENT_ID = "test-client-id"
API = "https://fantasy.premierleague.com/api"


def jwt(*, expires_in: float, issuer: str = ISSUER, client_id: str = CLIENT_ID) -> str:
    """A structurally real JWT. The signature is junk; nothing verifies it."""
    now = time.time()
    header = _b64({"alg": "RS256", "kid": "default"})
    payload = _b64(
        {
            "iat": now,
            "exp": now + expires_in,
            "iss": issuer,
            "client_id": client_id,
            "scope": "openid profile offline_access",
        }
    )
    return f"{header}.{payload}.not-a-real-signature"


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def session(*, access_in: float = 7200, refresh: str | None = "refresh-token-1") -> SessionCookies:
    cookies = {
        "access_token": jwt(expires_in=access_in),
        # Real jars carry these, and the bot protection cares about them.
        "datadome": "datadome-value",
        "cf_clearance": "cf-value",
    }
    if refresh is not None:
        cookies["refresh_token"] = refresh
    return SessionCookies(cookies=cookies)


@pytest.fixture
def auth(settings, tmp_path: Path) -> FPLAuthenticator:
    settings.state_dir = str(tmp_path / ".state")
    return FPLAuthenticator(settings)


def token_response(*, access_in: float = 28800, refresh: str | None = "refresh-token-2") -> dict:
    body: dict = {
        "access_token": jwt(expires_in=access_in),
        "token_type": "Bearer",
        "expires_in": int(access_in),
        "scope": "openid profile offline_access",
    }
    if refresh is not None:
        body["refresh_token"] = refresh
    return body


# ------------------------------------------------------------------ claim reading


def test_claims_are_read_without_verification():
    claims = decode_jwt_claims(jwt(expires_in=100))
    assert claims["iss"] == ISSUER
    assert claims["client_id"] == CLIENT_ID


@pytest.mark.parametrize("value", ["", "not-a-jwt", "a.b", "a.b.c", "...", "x.@@@@.y"])
def test_non_jwt_values_decode_to_nothing(value):
    """The legacy cookie scheme must flow through unharmed."""
    assert decode_jwt_claims(value) == {}


def test_token_url_and_client_come_from_the_token_itself():
    """An FPL-side move to a different PingOne environment needs no code change."""
    s = session()
    assert s.token_url == TOKEN_URL
    assert s.client_id == CLIENT_ID


def test_defaults_apply_when_the_token_says_nothing():
    s = SessionCookies(cookies={"access_token": "opaque-not-a-jwt", "refresh_token": "r"})
    assert s.client_id == DEFAULT_CLIENT_ID
    assert s.token_url.endswith("/as/token")


# --------------------------------------------------------------------- staleness


def test_expiry_comes_from_the_token_not_the_clock():
    """A freshly *fetched* but already-expired token is stale immediately."""
    fresh_fetch_dead_token = SessionCookies(
        cookies={"access_token": jwt(expires_in=-60), "refresh_token": "r"},
        obtained_at=time.time(),
    )
    assert fresh_fetch_dead_token.is_stale is True
    assert fresh_fetch_dead_token.is_expiring() is True


def test_a_long_lived_token_is_not_stale_even_when_the_blob_is_old():
    old_fetch_live_token = SessionCookies(
        cookies={"access_token": jwt(expires_in=7200)},
        obtained_at=time.time() - 86400,
    )
    assert old_fetch_live_token.is_stale is False


def test_the_refresh_window_opens_before_expiry():
    """Refresh early: a token that dies mid-request is a failed gameweek."""
    assert session(access_in=120).is_expiring(skew_seconds=300) is True
    assert session(access_in=600).is_expiring(skew_seconds=300) is False


def test_legacy_cookies_fall_back_to_wall_clock():
    legacy = SessionCookies(cookies={"pl_profile": "x", "sessionid": "y"})
    assert legacy.is_oauth is False
    assert legacy.is_stale is False
    aged = SessionCookies(
        cookies={"pl_profile": "x", "sessionid": "y"}, obtained_at=time.time() - 60 * 60 * 13
    )
    assert aged.is_stale is True


def test_describe_never_leaks_a_token():
    text = session().describe()
    assert "refresh-token-1" not in text
    assert jwt(expires_in=1)[:20] not in text
    assert "expires in" in text


# ----------------------------------------------------------------------- refresh


@respx.mock
def test_refresh_exchanges_the_token_and_persists_the_result(auth):
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))
    auth.cache.save(session(access_in=-60))

    refreshed = auth.get_session_cookies()

    assert route.call_count == 1
    assert refreshed.is_expiring() is False
    # Persisted, so the next process start doesn't refresh again.
    assert auth.cache.load().access_token == refreshed.access_token


@respx.mock
def test_refresh_posts_the_public_client_form(auth):
    """client_id, no secret -- FPL's OAuth client is public."""
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))
    auth.cache.save(session(access_in=-60))

    auth.get_session_cookies()

    request = route.calls[0].request
    body = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "refresh-token-1"
    assert body["client_id"] == CLIENT_ID
    assert "client_secret" not in body
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"


@respx.mock
def test_a_rotated_refresh_token_replaces_the_old_one(auth):
    """PingOne rotates on use: miss this and the next refresh presents a spent token."""
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))
    auth.cache.save(session(access_in=-60))

    refreshed = auth.get_session_cookies()

    assert refreshed.refresh_token == "refresh-token-2"
    assert auth.cache.load().refresh_token == "refresh-token-2"


@respx.mock
def test_an_omitted_refresh_token_keeps_the_existing_one(auth):
    """Some responses don't rotate; that means keep using what we have."""
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=token_response(refresh=None))
    )
    auth.cache.save(session(access_in=-60))

    assert auth.get_session_cookies().refresh_token == "refresh-token-1"


@respx.mock
def test_refresh_preserves_the_rest_of_the_cookie_jar(auth):
    """datadome and cf_clearance are what the bot protection reads."""
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))
    auth.cache.save(session(access_in=-60))

    refreshed = auth.get_session_cookies()

    assert refreshed.cookies["datadome"] == "datadome-value"
    assert refreshed.cookies["cf_clearance"] == "cf-value"
    assert "datadome=datadome-value" in refreshed.as_header()


@respx.mock
def test_a_valid_cached_token_is_not_refreshed(auth):
    route = respx.post(TOKEN_URL)
    auth.cache.save(session(access_in=7200))

    auth.get_session_cookies()

    assert route.call_count == 0, "refreshing a live token wastes a round trip"


@respx.mock
def test_force_refresh_bypasses_a_valid_cache(auth):
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))
    auth.cache.save(session(access_in=7200))

    auth.get_session_cookies(force_refresh=True)

    assert route.call_count == 1


@respx.mock
def test_an_expiring_pasted_header_is_refreshed_immediately(auth):
    """A header pasted hours ago is already half spent by the time it's used."""
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))
    auth.settings.fpl_cookie_header = SecretStr(session(access_in=-30).as_header())

    result = auth.get_session_cookies()

    assert route.call_count == 1
    assert result.is_expiring() is False


# ---------------------------------------------------------------------- failures


@respx.mock
def test_a_rejected_refresh_token_says_how_to_recover(auth):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Failed to decode"}
        )
    )
    auth.cache.save(session(access_in=-60))
    auth.settings.fpl_cookie_header = SecretStr("")

    with pytest.raises(FPLAuthError) as excinfo:
        auth.get_session_cookies()
    assert "re-paste" in str(excinfo.value).lower()


@respx.mock
def test_a_failed_refresh_falls_back_to_the_pasted_header(auth):
    """The env var may already hold a newer session than the cache."""
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    auth.cache.save(session(access_in=-60, refresh="spent-token"))
    good = session(access_in=7200, refresh="refresh-token-9")
    auth.settings.fpl_cookie_header = SecretStr(good.as_header())

    result = auth.get_session_cookies()

    assert result.refresh_token == "refresh-token-9"
    assert result.is_expiring() is False


@respx.mock
def test_a_network_failure_during_refresh_is_reported_clearly(auth):
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("dns is down"))
    auth.cache.save(session(access_in=-60))
    auth.settings.fpl_cookie_header = SecretStr("")

    with pytest.raises(FPLTokenRefreshError, match="refresh request failed"):
        auth.get_session_cookies()


@respx.mock
def test_a_response_without_an_access_token_is_an_error(auth):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"token_type": "Bearer"}))
    auth.cache.save(session(access_in=-60))
    auth.settings.fpl_cookie_header = SecretStr("")

    with pytest.raises(FPLTokenRefreshError, match="no access_token"):
        auth.get_session_cookies()


def test_refreshing_without_a_refresh_token_is_refused(auth):
    auth.cache.save(session(access_in=-60, refresh=None))
    auth.settings.fpl_cookie_header = SecretStr("")

    with pytest.raises(FPLTokenRefreshError, match="Nothing to refresh"):
        auth.refresh_now()


def test_no_credentials_at_all_names_the_cookie_header_first(auth):
    """The cookie header is now the normal way in, so mention it first."""
    with pytest.raises(FPLAuthError, match="FPL_COOKIE_HEADER"):
        auth.get_session_cookies()


# ------------------------------------------------------------------------ peek


def test_peek_reads_the_cache_without_network(auth):
    auth.cache.save(session(access_in=1200))
    peeked = auth.peek()
    assert peeked is not None and peeked.is_oauth


def test_peek_falls_back_to_the_env_header(auth):
    auth.settings.fpl_cookie_header = SecretStr(session().as_header())
    assert auth.peek() is not None


def test_peek_is_none_with_nothing_configured(auth):
    assert auth.peek() is None


# ------------------------------------------------------ the client's use of it


@respx.mock
def test_the_client_refreshes_before_a_request_rather_than_after_a_403(auth, settings):
    """Proactive: an expired token should never reach FPL in the first place."""
    settings.state_dir = auth.settings.state_dir
    auth.cache.save(session(access_in=-60))
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=token_response())
    )
    team_route = respx.get(f"{API}/my-team/999999/").mock(
        return_value=httpx.Response(200, json={"picks": [], "chips": [], "transfers": {}})
    )

    FPLClient(settings, authenticator=auth).my_team()

    assert token_route.call_count == 1
    sent = team_route.calls[0].request.headers["authorization"]
    assert sent == f"Bearer {auth.cache.load().access_token}"


@respx.mock
def test_the_client_does_not_reuse_a_token_that_expired_mid_process(auth, settings):
    """A process that lives for weeks must not pin the token it started with."""
    settings.state_dir = auth.settings.state_dir
    auth.cache.save(session(access_in=7200))
    team_route = respx.get(f"{API}/my-team/999999/").mock(
        return_value=httpx.Response(200, json={"picks": [], "chips": [], "transfers": {}})
    )
    client = FPLClient(settings, authenticator=auth)
    client.my_team()
    first = team_route.calls[0].request.headers["authorization"]

    # Time passes; the cached token is now dead and a refresh is available.
    auth.cache.save(session(access_in=-60, refresh="refresh-token-1"))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_response()))

    client.my_team()

    second = team_route.calls[-1].request.headers["authorization"]
    assert second != first, "the second request must carry the refreshed token"
