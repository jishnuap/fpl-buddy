"""The Discord bot connection sequence.

Regression coverage for a real bug: ``bot.start()`` bundles ``login()`` +
``connect()``, and running that as one background task while immediately
checking ``wait_until_ready()`` is a race -- readiness can be checked before
``login()`` has set up the state it depends on, which only ever surfaced
against a live token because nothing exercised this path offline.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr

from fpl_buddy.main import _connect_discord_bot


class _FakeBot:
    """Mimics the two-call shape (login then connect) without any network."""

    def __init__(self, *, ready_after_login: bool = True) -> None:
        self.calls: list[str] = []
        self._ready = asyncio.Event()
        self._ready_after_login = ready_after_login

    async def login(self, token: str) -> None:
        self.calls.append(f"login:{token}")
        if self._ready_after_login:
            self._ready.set()

    async def connect(self) -> None:
        self.calls.append("connect")
        await asyncio.Event().wait()  # the real gateway loop never returns

    async def wait_until_ready(self) -> None:
        await self._ready.wait()


async def test_login_happens_before_connect_is_backgrounded():
    bot = _FakeBot()
    await _connect_discord_bot(bot, "test-token")
    assert bot.calls[0] == "login:test-token"


async def test_returns_once_ready_without_waiting_for_connect_to_finish():
    """connect() never returns (it's the gateway loop) -- this must not block on it."""
    bot = _FakeBot()
    await asyncio.wait_for(_connect_discord_bot(bot, "test-token"), timeout=1)


async def test_a_bot_that_never_becomes_ready_times_out_with_an_actionable_message():
    bot = _FakeBot(ready_after_login=False)
    with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN"):
        await _connect_discord_bot(bot, "test-token", timeout=0.05)


# --------------------------------------------------------------- process shape


def _built_with(settings, monkeypatch):
    """build_app() against injected settings, with no network in the constructor."""
    from fpl_buddy import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return main.build_app()


def test_the_scheduler_runs_in_process_by_default(settings, monkeypatch):
    app = _built_with(settings, monkeypatch)
    assert app.state.scheduler is not None


def test_disabling_the_scheduler_leaves_a_plain_web_server(settings, monkeypatch):
    """The scale-to-zero deployment: `fpl-buddy tick` drives the schedule, and
    nothing in this process is allowed to keep the container alive."""
    settings.scheduler_enabled = False

    app = _built_with(settings, monkeypatch)

    assert app.state.scheduler is None
    assert app.state.discord_bot is None


def test_no_gateway_bot_when_the_scheduler_is_off(settings, monkeypatch):
    """A gateway WebSocket pins a container up exactly like the scheduler does,
    so one switch has to govern both -- otherwise the service still can't idle."""
    from fpl_buddy.discord_bot.rest import DiscordRestNotifier

    settings.notify_channel = "discord"
    settings.discord_bot_token = SecretStr("token")
    settings.discord_channel_id = 42
    settings.scheduler_enabled = False

    app = _built_with(settings, monkeypatch)

    assert app.state.discord_bot is None
    assert isinstance(app.state.orchestrator.notifier, DiscordRestNotifier)


def test_health_says_which_scheduler_is_in_charge(settings, monkeypatch):
    """A deployment that scaled to zero with the scheduler still on looks
    healthy and quietly never commits. This is how you tell from outside."""
    from fastapi.testclient import TestClient

    settings.scheduler_enabled = False
    with TestClient(_built_with(settings, monkeypatch)) as client:
        assert client.get("/health").json()["scheduler"] == "external"
