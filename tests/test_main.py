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
