"""Container entrypoint: the API and, by default, the scheduler in one process.

One process is the right shape when you have somewhere to run it. The work is a
couple of HTTP calls and one LLM run per gameweek, and keeping the scheduler
beside the API means the approval link and the deadline job read the same store
with no coordination.

Two things in here keep a container alive: the in-process scheduler, and the
Discord gateway's WebSocket. ``SCHEDULER_ENABLED=false`` turns off both, leaving
a stateless web server that can scale to zero -- for the deployment where
``fpl-buddy tick`` drives the schedule from a platform cron instead. That switch
governs both because they share one reason for existing, and turning off only
half of it would produce a service that still could not idle.

With the scheduler on: run exactly one instance, and don't let it scale to zero
-- a stopped container has no scheduler, so nothing commits at the deadline.
See ``docs/deployment.md`` and ``docs/serverless.md``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn

from .api import create_app
from .config import get_settings
from .notify import build_notifier
from .orchestrator import Orchestrator
from .scheduler import FplScheduler

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # These are chatty at INFO and say nothing useful here.
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _connect_discord_bot(bot, token: str, *, timeout: float = 30) -> None:
    """Log in, background the gateway loop, and don't return until ready.

    ``bot.start()`` is a shorthand for ``login()`` then ``connect()`` -- calling
    it as a single background task and immediately checking
    ``wait_until_ready()`` is a real race: readiness can be checked before
    ``login()`` has even set up the state it depends on, raising
    ``RuntimeError: Client has not been properly initialised``. Awaiting
    ``login()`` directly avoids it; only the long-lived ``connect()`` loop goes
    on a background task.
    """
    await bot.login(token)
    asyncio.create_task(bot.connect())
    try:
        await asyncio.wait_for(bot.wait_until_ready(), timeout=timeout)
    except TimeoutError as exc:
        raise RuntimeError(
            f"Discord bot did not become ready within {timeout:.0f}s -- "
            "check DISCORD_BOT_TOKEN."
        ) from exc


def build_app():
    settings = get_settings()
    bot = None
    if settings.notify_channel == "discord":
        if not settings.has_discord:
            raise RuntimeError(
                "NOTIFY_CHANNEL=discord needs both DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID."
            )
        if settings.scheduler_enabled:
            from .discord_bot.bot import build_bot as _make_bot

            # The bot's button callbacks need the orchestrator, and the
            # orchestrator's notifier needs the bot to post through -- so the
            # bot is built first without one, and given it once it exists.
            bot = _make_bot(settings, orchestrator=None)

    # No bot means build_notifier falls back to posting over HTTPS, which is
    # what a scale-to-zero web service needs.
    orchestrator = Orchestrator(settings, notifier=build_notifier(settings, discord_bot=bot))
    if bot is not None:
        bot.orchestrator = orchestrator

    scheduler = FplScheduler(settings, orchestrator=orchestrator) if settings.scheduler_enabled else None

    @asynccontextmanager
    async def lifespan(app):
        if bot is not None:
            await _connect_discord_bot(bot, settings.discord_bot_token.get_secret_value())
            logger.info("Discord bot connected; notifications will post to channel %s.",
                        settings.discord_channel_id)
        if scheduler is not None:
            scheduler.start()
        else:
            logger.info(
                "SCHEDULER_ENABLED=false: serving the API only. Something external "
                "must run `fpl-buddy tick`, or no proposal is ever made or committed."
            )
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown()
            if bot is not None:
                await bot.close()

    app = create_app(settings, orchestrator=orchestrator)
    app.router.lifespan_context = lifespan
    app.state.scheduler = scheduler
    app.state.discord_bot = bot
    return app


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logging.getLogger(__name__).info(
        "Starting fpl-buddy on port %s (dry_run=%s, auto_commit=%s, entry=%s, scheduler=%s).",
        settings.port, settings.dry_run, settings.auto_commit_enabled, settings.fpl_entry_id,
        "in-process" if settings.scheduler_enabled else "external (fpl-buddy tick)",
    )
    uvicorn.run(build_app(), host="0.0.0.0", port=settings.port, log_level="warning")


if __name__ == "__main__":
    run()
