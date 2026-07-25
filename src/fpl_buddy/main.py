"""Container entrypoint: the API and the scheduler in one process.

One process is the right shape here. The work is a couple of HTTP calls and one
LLM run per gameweek, and keeping the scheduler beside the API means the approval
link and the deadline job read the same store with no coordination.

Scale this to more than one replica and both replicas would propose and commit.
Run exactly one instance, and don't let it scale to zero -- a stopped container
has no scheduler, so nothing commits at the deadline. See ``docs/deployment.md``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn

from .api import create_app
from .config import get_settings
from .scheduler import FplScheduler


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # These are chatty at INFO and say nothing useful here.
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_app():
    settings = get_settings()
    scheduler = FplScheduler(settings)

    @asynccontextmanager
    async def lifespan(app):
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()

    app = create_app(settings, orchestrator=scheduler.orchestrator)
    app.router.lifespan_context = lifespan
    app.state.scheduler = scheduler
    return app


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logging.getLogger(__name__).info(
        "Starting fpl-buddy on port %s (dry_run=%s, auto_commit=%s, entry=%s).",
        settings.port, settings.dry_run, settings.auto_commit_enabled, settings.fpl_entry_id,
    )
    uvicorn.run(build_app(), host="0.0.0.0", port=settings.port, log_level="warning")


if __name__ == "__main__":
    run()
