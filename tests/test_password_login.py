"""The headless password login.

This is the flow that makes the service self-healing: without it, a spent
refresh token means every scheduled run fails until a human pastes a new cookie
header. It is also a *private* flow -- five requests against Premier League's
PingOne DaVinci endpoints, held together by regexes over a login page -- so the
tests here pin the shape of every request we send and check that each step fails
loudly enough to say which half of the flow moved.

Nothing here touches the network: the session is injected, exactly as AIrsenal's
own implementation allows.
"""

from __future__ import annotations

import base64
import hashlib
import logging

import pytest
from pydantic import SecretStr

from fpl_buddy.fpl.login import (
    CLIENT_ID,
    LOGIN_URLS,
    STANDARD_CONNECTION_ID,
    FPLLoginError,
    password_login,
)

PASSWORD = "hunter2-not-in-any-log"
LOGIN_PAGE = (
    '<html><script>window.__DATA__ = {"accessToken":"davinci-token","other":1};</script>'
    '<form><input type="hidden" name="state" value="resume-state"/></form></html>'
)
CONNECTION_ID = "connection-from-signon"


class FakeResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", json=None, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._json = json

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


def _default(step: str) -> FakeResponse:
    return {
        "authorize": lambda: FakeResponse(text=LOGIN_PAGE),
        "start": lambda: FakeResponse(json={"interactionId": "interaction-1", "id": "resp-1"}),
        "poll": lambda: FakeResponse(json={"id": "resp-2"}),
        "submit": lambda: FakeResponse(json={"id": "resp-3", "connectionId": CONNECTION_ID}),
        "confirm": lambda: FakeResponse(json={"dvResponse": "dv-response-blob"}),
        "resume": lambda: FakeResponse(
            status_code=302,
            headers={"Location": "https://fantasy.premierleague.com/?code=auth-code-1&state=x"},
        ),
        "token": lambda: FakeResponse(
            json={
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "token_type": "Bearer",
            }
        ),
    }[step]()


class FakeSession:
    """Replays the five-step flow, with any single step overridable."""

    def __init__(self, *, overrides=None, cookies=None):
        self.overrides = overrides or {}
        # A real jar picks up PingOne's own session cookies alongside the one
        # cookie worth keeping.
        self.cookies = cookies if cookies is not None else {"datadome": "dd-1", "ST": "pingone"}
        self.calls: list[tuple[str, str, dict]] = []
        self._standard_posts = 0

    def get(self, url, params=None, **kwargs):
        self.calls.append(("GET", url, {"params": params, **kwargs}))
        return self._respond("authorize")

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._respond(self._step_for(url))

    def _step_for(self, url: str) -> str:
        if url == LOGIN_URLS["start"]:
            return "start"
        if url == LOGIN_URLS["login"].format(STANDARD_CONNECTION_ID):
            self._standard_posts += 1
            return "poll" if self._standard_posts == 1 else "submit"
        if url == LOGIN_URLS["resume"]:
            return "resume"
        if url == LOGIN_URLS["token"]:
            return "token"
        return "confirm"

    def _respond(self, step: str) -> FakeResponse:
        return self.overrides.get(step) or _default(step)

    # -------------------------------------------------------------- assertions
    def body(self, step: str) -> dict:
        """The payload we sent at a given step, whichever way it was encoded."""
        wanted = {
            "start": LOGIN_URLS["start"],
            "resume": LOGIN_URLS["resume"],
            "token": LOGIN_URLS["token"],
        }
        posts = [c for c in self.calls if c[0] == "POST"]
        if step in wanted:
            call = next(c for c in posts if c[1] == wanted[step])
        else:
            standard = [c for c in posts if c[1].endswith("/customHTMLTemplate")]
            call = {"poll": standard[0], "submit": standard[1], "confirm": standard[2]}[step]
        return call[2].get("json") or call[2].get("data") or {}

    def headers(self, step: str) -> dict:
        posts = [c for c in self.calls if c[0] == "POST"]
        standard = [c for c in posts if c[1].endswith("/customHTMLTemplate")]
        call = {"poll": standard[0], "submit": standard[1], "confirm": standard[2]}[step]
        return call[2].get("headers") or {}


@pytest.fixture
def creds(settings):
    settings.fpl_email = "manager@example.test"
    settings.fpl_password = SecretStr(PASSWORD)
    return settings


# ----------------------------------------------------------------- happy path


def test_login_returns_the_minted_tokens(creds):
    session = password_login(creds, session=FakeSession())

    assert session.access_token == "new-access-token"
    assert session.refresh_token == "new-refresh-token"
    assert session.can_refresh, "offline_access is requested so refresh must keep working"


def test_login_carries_the_bot_protection_cookie_but_not_the_provider_session(creds):
    """datadome is what fantasy.premierleague.com's edge reads; PingOne's own
    session cookies belong to a host we never call again."""
    session = password_login(creds, session=FakeSession())

    assert session.cookies["datadome"] == "dd-1"
    assert "ST" not in session.cookies


def test_login_survives_an_unreadable_cookie_jar(creds):
    """Losing the jar is not a reason to throw away a session we just minted."""
    session = password_login(creds, session=FakeSession(cookies=object()))
    assert session.access_token == "new-access-token"


def test_login_works_without_a_refresh_token(creds):
    """A login-per-run still works; it just costs a full login each time."""
    fake = FakeSession(overrides={"token": FakeResponse(json={"access_token": "a"})})
    session = password_login(creds, session=fake)

    assert session.access_token == "a"
    assert session.can_refresh is False


# -------------------------------------------------------------- request shapes


def test_the_authorize_request_asks_for_offline_access_with_pkce(creds):
    fake = FakeSession()
    password_login(creds, session=fake)

    params = fake.calls[0][2]["params"]
    assert fake.calls[0][1] == LOGIN_URLS["auth"]
    assert params["client_id"] == CLIENT_ID
    assert params["response_type"] == "code"
    assert params["code_challenge_method"] == "S256"
    assert "offline_access" in params["scope"]


def test_the_code_challenge_matches_the_verifier_sent_at_the_exchange(creds):
    """PKCE is the only thing proving we redeemed our own code -- the client is
    public and there is no secret behind it."""
    fake = FakeSession()
    password_login(creds, session=fake)

    verifier = fake.body("token")["code_verifier"]
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    assert fake.calls[0][2]["params"]["code_challenge"] == expected


def test_the_credentials_go_to_the_sign_on_step_with_the_interaction_id(creds):
    fake = FakeSession()
    password_login(creds, session=fake)

    submitted = fake.body("submit")["parameters"]
    assert submitted["username"] == "manager@example.test"
    assert submitted["password"] == PASSWORD
    assert fake.headers("submit")["interactionId"] == "interaction-1"


def test_the_confirm_step_goes_to_the_connection_from_the_sign_on(creds):
    fake = FakeSession()
    password_login(creds, session=fake)

    confirm_url = [c[1] for c in fake.calls if c[1].endswith("/customHTMLTemplate")][-1]
    assert CONNECTION_ID in confirm_url


def test_resume_does_not_follow_the_redirect_that_carries_the_code(creds):
    """Follow the hop and the authorization code is gone."""
    fake = FakeSession()
    password_login(creds, session=fake)

    resume = next(c for c in fake.calls if c[1] == LOGIN_URLS["resume"])
    assert resume[2]["allow_redirects"] is False
    assert fake.body("resume")["state"] == "resume-state", "must echo the provider's state"


def test_the_exchange_sends_no_client_secret(creds):
    fake = FakeSession()
    password_login(creds, session=fake)

    body = fake.body("token")
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "auth-code-1"
    assert "client_secret" not in body


def test_the_password_is_never_logged(creds, caplog):
    with caplog.at_level(logging.DEBUG):
        password_login(creds, session=FakeSession())

    assert PASSWORD not in caplog.text
    assert "new-access-token" not in caplog.text


# -------------------------------------------------------------------- failures


def test_no_credentials_is_refused_before_any_request(creds):
    creds.fpl_password = SecretStr("")
    fake = FakeSession()

    with pytest.raises(FPLLoginError, match="FPL_EMAIL and FPL_PASSWORD"):
        password_login(creds, session=fake)
    assert fake.calls == []


def test_a_blocked_authorize_names_the_bot_protection(creds):
    """The expected failure from a datacenter IP, and the operator needs to know
    that no credential change will fix it."""
    fake = FakeSession(overrides={"authorize": FakeResponse(status_code=403, text="blocked")})

    with pytest.raises(FPLLoginError, match="bot protection") as excinfo:
        password_login(creds, session=fake)
    assert "FPL_COOKIE_HEADER" in str(excinfo.value)
    assert excinfo.value.step == "authorize"


@pytest.mark.parametrize(
    ("broken", "response", "step", "message"),
    [
        ("authorize", FakeResponse(text="<html>nope</html>"), "authorize", "no accessToken"),
        ("authorize", FakeResponse(text='{"accessToken":"t"}'), "authorize", "no state field"),
        ("start", FakeResponse(text="<html>error</html>"), "start", "not JSON"),
        ("start", FakeResponse(json={"id": "r"}), "start", "interactionId"),
        ("poll", FakeResponse(json={}), "sign-on poll", "no id in the poll response"),
        (
            "submit",
            FakeResponse(json={"id": "r"}),
            "sign-on submit",
            "rejected the email or password",
        ),
        ("confirm", FakeResponse(json={"id": "r"}), "sign-on confirm", "no dvResponse"),
        ("resume", FakeResponse(status_code=302, headers={}), "resume", "no authorization code"),
        ("token", FakeResponse(json={"token_type": "Bearer"}), "token exchange", "no access_token"),
    ],
)
def test_every_step_fails_by_name(creds, broken, response, step, message):
    """A private flow breaks eventually; the logs must say which half moved."""
    fake = FakeSession(overrides={broken: response})

    with pytest.raises(FPLLoginError, match=message) as excinfo:
        password_login(creds, session=fake)
    assert excinfo.value.step == step
    assert step in str(excinfo.value)
