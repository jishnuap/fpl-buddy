"""FPL auth and HTTP client, fully mocked with respx.

The write payload tests are the ones that matter most: they pin the exact JSON
shape and headers that will one day be sent for real. If FPL changes its API, or
someone "tidies" a field name, these fail before your points do.

They are not a substitute for diffing against a real browser capture -- see
docs/verify-payloads.md.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from fpl_buddy.fpl.auth import (
    COOKIE_MAX_AGE_SECONDS,
    CookieCache,
    FPLAuthenticator,
    FPLAuthError,
    SessionCookies,
    parse_cookie_header,
)
from fpl_buddy.fpl.client import (
    UNLIMITED_FREE_TRANSFERS,
    FPLApiError,
    FPLClient,
    TransferRejected,
    parse_bootstrap,
    parse_my_team,
)

from .conftest import DEF_ARS, FREE_MID_NEW, FWD_CAPTAIN, MID_LIV, MID_VICE, load_json

API = "https://fantasy.premierleague.com/api"
LOGIN = "https://users.premierleague.com/accounts/login/"
COOKIE = "pl_profile=eyJhIjoxfQ; sessionid=abc123; other=keepme"


@pytest.fixture
def authed_settings(settings):
    settings.fpl_cookie_header = _secret(COOKIE)
    settings.fpl_entry_id = 999999
    return settings


def _secret(value: str):
    from pydantic import SecretStr

    return SecretStr(value)


# ------------------------------------------------------------------ cookie plumbing


def test_cookie_header_is_parsed_into_pairs():
    cookies = parse_cookie_header("a=1; b=2;  c=three=with=equals ; ; junk")
    assert cookies == {"a": "1", "b": "2", "c": "three=with=equals"}


def test_session_is_complete_only_with_both_required_cookies():
    assert SessionCookies(cookies={"pl_profile": "x", "sessionid": "y"}).is_complete
    assert not SessionCookies(cookies={"sessionid": "y"}).is_complete
    assert not SessionCookies(cookies={"pl_profile": "x", "sessionid": ""}).is_complete


def test_session_header_round_trips_every_cookie():
    session = SessionCookies(cookies=parse_cookie_header(COOKIE))
    assert session.as_header() == COOKIE
    assert "other=keepme" in session.as_header(), "unrelated cookies must be preserved"


def test_session_goes_stale():
    fresh = SessionCookies(cookies={"pl_profile": "x", "sessionid": "y"})
    old = SessionCookies(
        cookies={"pl_profile": "x", "sessionid": "y"},
        obtained_at=time.time() - COOKIE_MAX_AGE_SECONDS - 1,
    )
    assert not fresh.is_stale
    assert old.is_stale


def test_cookie_cache_round_trip(tmp_path: Path):
    cache = CookieCache(tmp_path / "nested" / "cookies.json")
    assert cache.load() is None

    session = SessionCookies(cookies={"pl_profile": "x", "sessionid": "y"})
    cache.save(session)
    loaded = cache.load()
    assert loaded is not None and loaded.cookies == session.cookies
    assert cache.path.stat().st_mode & 0o777 == 0o600, "cookies are a credential"

    cache.clear()
    assert cache.load() is None


def test_corrupt_cookie_cache_is_ignored(tmp_path: Path):
    path = tmp_path / "cookies.json"
    path.write_text("{not json")
    assert CookieCache(path).load() is None


# ------------------------------------------------------------------------- auth


def test_pasted_cookie_header_is_used_and_cached(authed_settings):
    auth = FPLAuthenticator(authed_settings)
    session = auth.get_session_cookies()
    assert session.cookies["sessionid"] == "abc123"
    assert auth.cache.load() is not None


def test_incomplete_cookie_header_is_rejected_loudly(settings):
    settings.fpl_cookie_header = _secret("sessionid=abc123")
    with pytest.raises(FPLAuthError, match="pl_profile"):
        FPLAuthenticator(settings).get_session_cookies()


def test_no_credentials_at_all_is_an_error(settings):
    with pytest.raises(FPLAuthError, match="No FPL credentials"):
        FPLAuthenticator(settings).get_session_cookies()


def _with_credentials(settings, monkeypatch, result):
    """Credentials set, and the password login stubbed to ``result``.

    ``result`` is either a SessionCookies to return or an exception to raise.
    The flow itself is covered in test_password_login.py; what matters here is
    when the authenticator reaches for it.
    """
    from fpl_buddy.fpl import login as login_module

    settings.fpl_email = "me@example.test"
    settings.fpl_password = _secret("hunter2")

    calls: list[int] = []

    def fake_login(_settings, **_kwargs):
        calls.append(1)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(login_module, "password_login", fake_login)
    return calls


def test_credentials_mint_a_session_when_nothing_is_cached(settings, monkeypatch):
    minted = SessionCookies(cookies={"access_token": "minted", "refresh_token": "r"})
    calls = _with_credentials(settings, monkeypatch, minted)
    auth = FPLAuthenticator(settings)

    session = auth.get_session_cookies()

    assert session.access_token == "minted"
    assert len(calls) == 1
    assert auth.cache.load().access_token == "minted", "a login is worth caching"


def test_a_password_login_outranks_the_pasted_header(authed_settings, monkeypatch):
    """The header is a human-maintained fallback; credentials renew themselves."""
    minted = SessionCookies(cookies={"access_token": "minted", "refresh_token": "r"})
    _with_credentials(authed_settings, monkeypatch, minted)

    session = FPLAuthenticator(authed_settings).get_session_cookies()

    assert session.access_token == "minted"


def test_a_failed_login_still_falls_back_to_the_pasted_header(authed_settings, monkeypatch):
    """Blocked from this network is exactly when the header earns its keep."""
    from fpl_buddy.fpl.login import FPLLoginError

    _with_credentials(
        authed_settings, monkeypatch, FPLLoginError("authorize", "HTTP 403. bot protection")
    )

    session = FPLAuthenticator(authed_settings).get_session_cookies()

    assert session.cookies["sessionid"] == "abc123"


def test_a_failed_login_with_no_fallback_reports_the_login_error(settings, monkeypatch):
    from fpl_buddy.fpl.login import FPLLoginError

    _with_credentials(settings, monkeypatch, FPLLoginError("sign-on submit", "bad password"))

    with pytest.raises(FPLAuthError, match="sign-on submit"):
        FPLAuthenticator(settings).get_session_cookies()


# ------------------------------------------------------------------------ reads


@respx.mock
def test_bootstrap_is_parsed_and_cached(authed_settings):
    route = respx.get(f"{API}/bootstrap-static/").mock(
        return_value=httpx.Response(200, json=load_json("bootstrap-static.json"))
    )
    client = FPLClient(authed_settings)

    boot = client.bootstrap()
    assert boot.player(FWD_CAPTAIN).web_name == "Vasquez"
    assert boot.next_gameweek.id == 4

    client.bootstrap()
    assert route.call_count == 1, "second call should hit the cache"
    client.bootstrap(refresh=True)
    assert route.call_count == 2


@respx.mock
def test_fixtures_pass_the_event_filter(authed_settings):
    route = respx.get(f"{API}/fixtures/").mock(
        return_value=httpx.Response(200, json=load_json("fixtures.json"))
    )
    fixtures = FPLClient(authed_settings).fixtures(event=4)
    assert len(fixtures) == 3
    assert route.calls[0].request.url.params["event"] == "4"


@respx.mock
def test_future_fixtures_ask_for_the_whole_horizon(authed_settings):
    route = respx.get(f"{API}/fixtures/").mock(
        return_value=httpx.Response(200, json=load_json("fixtures-future.json"))
    )
    fixtures = FPLClient(authed_settings).fixtures(future=True)
    assert route.calls[0].request.url.params["future"] == "1"
    assert {f.event for f in fixtures} == {4, 5, 6, 7, 8}, "more than one gameweek"


@respx.mock
def test_an_explicit_event_wins_over_future(authed_settings):
    route = respx.get(f"{API}/fixtures/").mock(
        return_value=httpx.Response(200, json=load_json("fixtures.json"))
    )
    FPLClient(authed_settings).fixtures(event=4, future=True)
    params = route.calls[0].request.url.params
    assert params["event"] == "4"
    assert "future" not in params


def _stub_firecrawl(*, raw_html: str | None = None, error: Exception | None = None):
    """A stand-in for ``firecrawl.Firecrawl``, monkeypatched in place of the class.

    Returned as a class (not an instance), because production code calls
    ``Firecrawl(api_key=key)`` -- constructing it is part of what's under test.
    """
    calls: list[dict] = []

    class _Stub:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        def scrape(self, url: str, **kwargs):
            calls.append({"url": url, **kwargs})
            if error is not None:
                raise error
            return SimpleNamespace(raw_html=raw_html)

    _Stub.calls = calls  # type: ignore[attr-defined]
    return _Stub


@respx.mock
def test_a_403_read_falls_back_to_firecrawl(authed_settings, monkeypatch):
    """FPL's edge blocks the IP outright; Firecrawl fetches from its own IP pool."""
    authed_settings.firecrawl_api_key = _secret("fc-key")
    stub = _stub_firecrawl(raw_html=json.dumps(load_json("bootstrap-static.json")))
    monkeypatch.setattr("fpl_buddy.fpl.client.Firecrawl", stub)
    respx.get(f"{API}/bootstrap-static/").mock(return_value=httpx.Response(403, text=""))

    boot = FPLClient(authed_settings).bootstrap()

    assert boot.next_gameweek.id == 4
    assert stub.calls[0]["url"] == f"{API}/bootstrap-static/"
    assert "sessionid=abc123" in stub.calls[0]["headers"]["Cookie"], (
        "bootstrap() reads authorised -- the fallback should carry the same "
        "session headers the direct request did"
    )


@respx.mock
def test_a_403_without_a_firecrawl_key_still_raises(authed_settings):
    """No FIRECRAWL_API_KEY configured: behaviour is exactly what it was before."""
    respx.get(f"{API}/bootstrap-static/").mock(return_value=httpx.Response(403, text="blocked"))
    with pytest.raises(FPLApiError) as excinfo:
        FPLClient(authed_settings).bootstrap()
    assert excinfo.value.status_code == 403


@respx.mock
def test_a_blocked_read_leaves_the_cached_credentials_alone(authed_settings):
    """An edge block is not an expired session, and must not cost us the cache."""
    auth = FPLAuthenticator(authed_settings)
    auth.cache.save(SessionCookies(cookies={"access_token": "live", "refresh_token": "keep-me"}))
    route = respx.get(f"{API}/bootstrap-static/").mock(
        return_value=httpx.Response(403, text="blocked")
    )

    with pytest.raises(FPLApiError):
        FPLClient(authed_settings, authenticator=auth).bootstrap()

    assert route.call_count == 1, "retrying an IP block never helps"
    cached = auth.cache.load()
    assert cached.refresh_token == "keep-me"
    assert cached.access_token == "live"


@respx.mock
def test_a_403_that_names_the_credentials_does_re_authenticate(authed_settings):
    """FPL's own auth failure, which a fresh session genuinely can fix."""
    route = respx.get(f"{API}/bootstrap-static/").mock(
        side_effect=[
            httpx.Response(403, json={"detail": "Authentication credentials were not provided."}),
            httpx.Response(200, json=load_json("bootstrap-static.json")),
        ]
    )

    boot = FPLClient(authed_settings).bootstrap()

    assert boot.next_gameweek.id == 4
    assert route.call_count == 2


@respx.mock
def test_a_firecrawl_error_falls_through_to_the_original_403(authed_settings, monkeypatch):
    authed_settings.firecrawl_api_key = _secret("fc-key")
    stub = _stub_firecrawl(error=RuntimeError("firecrawl is down"))
    monkeypatch.setattr("fpl_buddy.fpl.client.Firecrawl", stub)
    respx.get(f"{API}/bootstrap-static/").mock(return_value=httpx.Response(403, text="blocked"))

    with pytest.raises(FPLApiError) as excinfo:
        FPLClient(authed_settings).bootstrap()
    assert excinfo.value.status_code == 403, "a broken fallback must not hide the real error"


@respx.mock
def test_unparsable_firecrawl_content_falls_through(authed_settings, monkeypatch):
    authed_settings.firecrawl_api_key = _secret("fc-key")
    stub = _stub_firecrawl(raw_html="<html>not json</html>")
    monkeypatch.setattr("fpl_buddy.fpl.client.Firecrawl", stub)
    respx.get(f"{API}/bootstrap-static/").mock(return_value=httpx.Response(403, text="blocked"))

    with pytest.raises(FPLApiError):
        FPLClient(authed_settings).bootstrap()


@respx.mock
def test_a_401_does_not_try_the_firecrawl_fallback(authed_settings, monkeypatch):
    """A 401 is an expired session, handled by the existing refresh-and-retry --

    not FPL's edge blocking the IP, so it's not something a different network
    would fix. Firecrawl must not be spent on it.
    """
    authed_settings.firecrawl_api_key = _secret("fc-key")
    stub = _stub_firecrawl(raw_html="{}")
    monkeypatch.setattr("fpl_buddy.fpl.client.Firecrawl", stub)
    respx.get(f"{API}/my-team/999999/").mock(return_value=httpx.Response(401, text="nope"))

    with pytest.raises(FPLApiError):
        FPLClient(authed_settings).my_team()
    assert stub.calls == []


@respx.mock
def test_set_piece_notes_are_fetched(authed_settings):
    payload = {"teams": [{"id": 1, "notes": [{"info_message": "Saka on pens"}]}]}
    respx.get(f"{API}/team/set-piece-notes/").mock(
        return_value=httpx.Response(200, json=payload)
    )
    assert FPLClient(authed_settings).set_piece_notes() == payload


# ------------------------------------------------------- the richer player model


def test_the_opta_underlying_numbers_are_parsed(authed_settings):
    """xG/xA come from the FPL API directly -- there is no scraper for this."""
    boot = parse_bootstrap(load_json("bootstrap-static.json"))
    striker = boot.player(FWD_CAPTAIN)
    assert striker.expected_goals_per_90 == 0.45
    assert striker.expected_goal_involvements_per_90 == 0.67
    assert striker.starts_per_90 == 1.0
    assert striker.ep_next == 4.1


def test_set_piece_order_becomes_a_readable_role(authed_settings):
    boot = parse_bootstrap(load_json("bootstrap-static.json"))
    assert boot.player(FWD_CAPTAIN).set_piece_duties == "P1", "first-choice penalties"
    assert boot.player(230).set_piece_duties == "F1", "first-choice direct free kicks"
    assert boot.player(231).set_piece_duties == "C1", "first-choice corners"
    assert boot.player(DEF_ARS).set_piece_duties == "", "takes nothing"


def test_nulls_in_the_numeric_fields_do_not_kill_the_parse():
    """One null in a 558-player payload must not cost a gameweek."""
    boot = parse_bootstrap(load_json("bootstrap-static.json"))
    blank = boot.player(611)
    assert blank.expected_goals_per_90 == 0.0
    assert blank.starts_per_90 == 0.0
    assert blank.ict_index == 0.0
    assert blank.form == 0.0
    assert blank.ep_next is None, "no projection is different from a projection of zero"


def test_team_strength_is_split_by_attack_defence_and_venue():
    boot = parse_bootstrap(load_json("bootstrap-static.json"))
    team = boot.team(1)
    assert team.strength_attack_home == 1291
    assert team.strength_defence_away == 1241
    assert team.has_strength_data is True
    assert team.form is None, "null pre-season, and that must parse"


def test_a_preseason_team_reports_no_strength_data():
    boot = parse_bootstrap(load_json("bootstrap-static.json"))
    team = boot.team(1)
    team.strength_attack_home = team.strength_attack_away = 0
    team.strength_defence_home = team.strength_defence_away = 0
    assert team.has_strength_data is False


@respx.mock
def test_my_team_sends_the_cookie_header(authed_settings):
    route = respx.get(f"{API}/my-team/999999/").mock(
        return_value=httpx.Response(200, json=load_json("my-team.json"))
    )
    my_team = FPLClient(authed_settings).my_team()

    assert len(my_team.picks) == 15
    assert my_team.bank == 15
    assert my_team.free_transfers == 1
    assert my_team.captain_id == FWD_CAPTAIN
    assert my_team.vice_captain_id == MID_VICE
    assert set(my_team.chips_available) == {"wildcard", "3xc"}
    assert my_team.active_chip is None

    headers = route.calls[0].request.headers
    assert "sessionid=abc123" in headers["cookie"]
    assert headers["x-requested-with"] == "XMLHttpRequest"


def test_my_team_without_an_entry_id_is_an_error(settings):
    settings.fpl_entry_id = 0
    with pytest.raises(FPLApiError, match="FPL_ENTRY_ID"):
        FPLClient(settings).my_team()


def test_unlimited_transfers_are_not_a_phantom_hit():
    """`transfers.limit` is None on a wildcard or pre-season, not 0."""
    raw = load_json("my-team.json")
    raw["transfers"]["limit"] = None
    assert parse_my_team(raw).free_transfers == UNLIMITED_FREE_TRANSFERS


def test_missing_transfer_block_defaults_safely():
    raw = load_json("my-team.json")
    raw.pop("transfers")
    parsed = parse_my_team(raw)
    assert parsed.bank == 0
    assert parsed.free_transfers == UNLIMITED_FREE_TRANSFERS


def test_active_chip_is_detected():
    raw = load_json("my-team.json")
    raw["chips"][0]["status_for_entry"] = "active"
    assert parse_my_team(raw).active_chip == "wildcard"


@respx.mock
def test_expired_session_is_refreshed_once_then_retried(authed_settings):
    route = respx.get(f"{API}/my-team/999999/").mock(
        side_effect=[
            httpx.Response(401, text="unauthorised"),
            httpx.Response(200, json=load_json("my-team.json")),
        ]
    )
    my_team = FPLClient(authed_settings).my_team()
    assert len(my_team.picks) == 15
    assert route.call_count == 2


@respx.mock
def test_persistent_401_surfaces_as_an_error(authed_settings):
    respx.get(f"{API}/my-team/999999/").mock(return_value=httpx.Response(401, text="nope"))
    with pytest.raises(FPLApiError) as excinfo:
        FPLClient(authed_settings).my_team()
    assert excinfo.value.status_code == 401


@respx.mock
def test_transport_errors_are_retried(authed_settings):
    route = respx.get(f"{API}/bootstrap-static/").mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.Response(200, json=load_json("bootstrap-static.json")),
        ]
    )
    assert FPLClient(authed_settings).bootstrap().next_gameweek.id == 4
    assert route.call_count == 2


@respx.mock
def test_verify_session_reports_health(authed_settings):
    respx.get(f"{API}/my-team/999999/").mock(
        return_value=httpx.Response(200, json=load_json("my-team.json"))
    )
    assert FPLClient(authed_settings).verify_session() is True


@respx.mock
def test_verify_session_is_false_when_rejected(authed_settings):
    respx.get(f"{API}/my-team/999999/").mock(return_value=httpx.Response(403, text="no"))
    assert FPLClient(authed_settings).verify_session() is False


@respx.mock
def test_verify_session_probes_the_squad_not_just_me(authed_settings):
    """Regression: /me/ answers 200 for a session that cannot read the squad.

    Since FPL moved to OAuth, a cookie jar with no usable access token still
    gets a 200 from /me/. Probing it reported a healthy session that would then
    fail at the deadline -- so the check has to hit the endpoint that matters.
    """
    me = respx.get(f"{API}/me/").mock(return_value=httpx.Response(200, json={"player": {}}))
    respx.get(f"{API}/my-team/999999/").mock(
        return_value=httpx.Response(
            403, json={"detail": "Authentication credentials were not provided."}
        )
    )

    assert FPLClient(authed_settings).verify_session() is False
    assert me.call_count == 0, "a 200 from /me/ must not be treated as proof"


@respx.mock
def test_verify_session_falls_back_to_me_without_an_entry_id(settings):
    """No entry id configured: /me/ is the only thing left to probe."""
    settings.fpl_cookie_header = _secret(COOKIE)
    settings.fpl_entry_id = 0
    respx.get(f"{API}/me/").mock(return_value=httpx.Response(200, json={"player": {}}))
    assert FPLClient(settings).verify_session() is True


# ----------------------------------------------------------------------- writes


TRANSFER = {
    "element_in": FREE_MID_NEW,
    "element_out": MID_LIV,
    "purchase_price": 50,
    "selling_price": 52,
}


@respx.mock
def test_dry_run_sends_nothing_at_all(authed_settings):
    assert authed_settings.dry_run is True
    route = respx.post(f"{API}/transfers/")
    picks_route = respx.post(f"{API}/my-team/999999/")

    client = FPLClient(authed_settings)
    result = client.submit_transfers(transfers=[TRANSFER], event=4)
    picks = client.submit_picks(picks=[{"element": 1, "position": 1}])

    assert result["dry_run"] is True
    assert result["payload"]["transfers"] == [TRANSFER]
    assert picks["dry_run"] is True
    assert route.call_count == 0 and picks_route.call_count == 0


@respx.mock
def test_transfer_payload_and_headers_are_exact(authed_settings):
    authed_settings.dry_run = False
    route = respx.post(f"{API}/transfers/").mock(return_value=httpx.Response(200, json={}))

    FPLClient(authed_settings).submit_transfers(transfers=[TRANSFER], event=4)

    request = route.calls[0].request
    payload = json.loads(request.content)
    assert payload == {
        "confirmed": True,
        "entry": 999999,
        "event": 4,
        "transfers": [TRANSFER],
        "chip": None,
        "freehit": False,
        "wildcard": False,
    }
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-requested-with"] == "XMLHttpRequest"
    assert request.headers["origin"] == "https://fantasy.premierleague.com"
    assert request.headers["referer"] == "https://fantasy.premierleague.com/transfers"
    assert "sessionid=abc123" in request.headers["cookie"]


@respx.mock
@pytest.mark.parametrize(
    ("chip", "freehit", "wildcard"),
    [(None, False, False), ("wildcard", False, True), ("freehit", True, False)],
)
def test_chip_flags_track_the_chip(authed_settings, chip, freehit, wildcard):
    authed_settings.dry_run = False
    route = respx.post(f"{API}/transfers/").mock(return_value=httpx.Response(200, json={}))

    FPLClient(authed_settings).submit_transfers(transfers=[TRANSFER], event=4, chip=chip)

    payload = json.loads(route.calls[0].request.content)
    assert (payload["chip"], payload["freehit"], payload["wildcard"]) == (chip, freehit, wildcard)


@respx.mock
def test_picks_payload_and_referer_are_exact(authed_settings):
    authed_settings.dry_run = False
    route = respx.post(f"{API}/my-team/999999/").mock(return_value=httpx.Response(200, json={}))

    picks = [
        {"element": 100 + i, "position": i + 1, "is_captain": i == 0, "is_vice_captain": i == 1}
        for i in range(15)
    ]
    FPLClient(authed_settings).submit_picks(picks=picks, chip="3xc")

    request = route.calls[0].request
    payload = json.loads(request.content)
    assert payload == {"picks": picks, "chip": "3xc"}
    assert len(payload["picks"]) == 15
    assert request.headers["referer"] == "https://fantasy.premierleague.com/my-team"


@respx.mock
def test_a_rejected_write_raises_with_the_reason(authed_settings):
    authed_settings.dry_run = False
    respx.post(f"{API}/transfers/").mock(
        return_value=httpx.Response(400, json={"non_form_errors": ["Not enough money"]})
    )
    with pytest.raises(TransferRejected, match="Not enough money"):
        FPLClient(authed_settings).submit_transfers(transfers=[TRANSFER], event=4)


@respx.mock
def test_a_redirected_write_is_a_failure_not_a_silent_success(authed_settings):
    """A 302 on a write means we got bounced to login and nothing was submitted.

    The body is empty, so anything that treats 3xx as success records a
    submission that never happened -- the worst outcome available.
    """
    authed_settings.dry_run = False
    respx.post(f"{API}/transfers/").mock(
        return_value=httpx.Response(302, headers={"location": "https://example.test/login"})
    )
    with pytest.raises(TransferRejected, match="session is probably dead"):
        FPLClient(authed_settings).submit_transfers(transfers=[TRANSFER], event=4)


@respx.mock
def test_a_redirected_read_is_a_failure_too(authed_settings):
    respx.get(f"{API}/bootstrap-static/").mock(
        return_value=httpx.Response(302, headers={"location": "https://example.test/login"})
    )
    with pytest.raises(FPLApiError):
        FPLClient(authed_settings).bootstrap()


@respx.mock
def test_write_retries_once_after_refreshing_a_dead_session(authed_settings):
    authed_settings.dry_run = False
    route = respx.post(f"{API}/transfers/").mock(
        side_effect=[
            httpx.Response(401, json={"detail": "Authentication credentials were not provided."}),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    result = FPLClient(authed_settings).submit_transfers(transfers=[TRANSFER], event=4)
    assert result == {"ok": 1}
    assert route.call_count == 2


@respx.mock
def test_a_blocked_write_is_not_treated_as_a_dead_session(authed_settings):
    """FPL's edge 403s writes from a datacenter IP whatever the credentials are.

    Retrying with renewed credentials cannot help, and doing so spends a
    single-use refresh token on a problem that has nothing to do with auth --
    which is how a deployment talks itself into needing a human.
    """
    authed_settings.dry_run = False
    route = respx.post(f"{API}/transfers/").mock(return_value=httpx.Response(403, text="blocked"))

    with pytest.raises(TransferRejected):
        FPLClient(authed_settings).submit_transfers(transfers=[TRANSFER], event=4)
    assert route.call_count == 1, "a bare 403 must not trigger a re-authentication"
