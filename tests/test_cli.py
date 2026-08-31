"""CLI surface.

Typer resolves argument signatures at call time, so a bad option declaration is
invisible until someone runs the command -- usually at the worst moment. These
tests walk every command's help and exercise the paths that need no network.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fpl_buddy import orchestrator as orchestrator_module
from fpl_buddy.cli import app
from fpl_buddy.config import get_settings
from fpl_buddy.data import context as context_module

runner = CliRunner()

COMMANDS = [
    "login", "verify", "context", "list", "show", "propose",
    "check", "approve", "reject", "amend", "commit", "schedule", "serve",
    "harvest", "articles",
]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch):
    """Point the CLI at a scratch state dir and away from any real .env."""
    for name in (
        "FPL_EMAIL", "FPL_PASSWORD", "FPL_COOKIE_HEADER", "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT", "NOTIFY_CHANNEL", "WEBHOOK_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("FPL_ENTRY_ID", "999999")
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.chdir(tmp_path)  # so no developer .env is picked up
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_root_help_lists_every_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_each_command_has_working_help(command):
    """Catches malformed typer signatures without running anything."""
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output


def test_no_arguments_shows_help_rather_than_an_error():
    result = runner.invoke(app, [])
    assert "Usage:" in result.output


def test_list_with_no_proposals_says_so():
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No proposals stored yet" in result.output


def test_show_with_no_proposals_fails_cleanly():
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 1
    assert "No proposals stored yet" in result.output


def test_show_of_an_unknown_id_fails_cleanly():
    result = runner.invoke(app, ["show", "gw04-nope"])
    assert result.exit_code == 1
    assert "No such proposal" in result.output


def test_approve_with_no_proposals_fails_cleanly():
    result = runner.invoke(app, ["approve"])
    assert result.exit_code == 1
    assert "No proposals stored yet" in result.output


def test_check_with_no_proposals_fails_cleanly():
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1


def test_amend_requires_a_note():
    result = runner.invoke(app, ["amend"])
    assert result.exit_code != 0


def test_list_and_show_render_a_stored_proposal(tmp_path: Path, context):
    """Write a proposal through the real store, then read it back via the CLI."""
    from fpl_buddy.decisions.store import build_store

    from .conftest import make_proposal, make_stored

    settings = get_settings()
    store = build_store(settings)
    store.save(make_stored(make_proposal(), context, id="gw04-testing"))

    listed = runner.invoke(app, ["list"])
    assert listed.exit_code == 0
    assert "gw04-testing" in listed.output
    assert "pending" in listed.output

    shown = runner.invoke(app, ["show", "gw04-testing"])
    assert shown.exit_code == 0
    assert "Vasquez" in shown.output
    assert "Captain" in shown.output


def test_show_can_print_the_brief_and_transcript(context):
    from fpl_buddy.decisions.store import build_store

    from .conftest import make_proposal, make_stored

    store = build_store(get_settings())
    store.save(
        make_stored(
            make_proposal(),
            context,
            id="gw04-audit",
            context_snapshot="THE BRIEF GOES HERE",
            agent_transcript="[ai] weighing the options",
        )
    )

    result = runner.invoke(app, ["show", "gw04-audit", "--brief", "--transcript"])
    assert result.exit_code == 0
    assert "THE BRIEF GOES HERE" in result.output
    assert "weighing the options" in result.output


def test_dry_run_banner_is_shown_before_anything_risky(monkeypatch):
    """`propose` must announce the mode before it does any work."""

    def explode(self):
        raise RuntimeError("stop here")

    monkeypatch.setattr(orchestrator_module.Orchestrator, "propose", explode, raising=True)
    result = runner.invoke(app, ["propose"])
    assert "DRY RUN" in result.output


def test_live_mode_is_announced_loudly(monkeypatch):

    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()

    def explode(self):
        raise RuntimeError("stop here")

    monkeypatch.setattr(orchestrator_module.Orchestrator, "propose", explode, raising=True)
    result = runner.invoke(app, ["propose"])
    assert "LIVE" in result.output


# ----------------------------------------------------------------- knowledge


def test_harvest_without_sources_configured_explains_itself():
    result = runner.invoke(app, ["harvest"])
    assert result.exit_code != 0
    assert "KNOWLEDGE_SOURCES_FILE" in result.output


def test_articles_says_so_when_the_store_is_empty():
    result = runner.invoke(app, ["articles"])
    assert result.exit_code == 0
    assert "No harvested articles" in result.output


def test_the_brief_is_printed_verbatim_not_as_rich_markup(monkeypatch, tmp_path):
    """Square brackets in the brief are data, not style tags.

    Rich reads `[article-id]` as a markup tag and silently swallows it, which
    broke the one command whose whole job is to show the brief exactly as the
    agent will see it. The squad table's `[GKP, id=110]` is exposed to the same
    thing.
    """
    from fpl_buddy import cli

    marker = "[some-article-id] | [GKP, id=110]"

    class _FakeContext:
        def render(self) -> str:
            return f"# brief\n{marker}\n"

    monkeypatch.setattr(context_module, "build_context", lambda *a, **k: _FakeContext())
    monkeypatch.setattr(cli, "FPLClient", lambda *a, **k: object())

    result = runner.invoke(app, ["context"])
    assert result.exit_code == 0
    assert "some-article-id" in result.output
    assert "GKP, id=110" in result.output


def test_the_cli_does_not_import_the_agent_stack():
    """`tick` runs every few minutes and mostly decides nothing is due.

    Importing the CLI used to pull `.orchestrator` at module scope, and with it
    LangChain, deepagents and langchain-openai -- about a second on a laptop and
    the best part of twenty on a cold 1-vCPU container, paid on every scheduled
    run whether or not anything happened. tick.py keeps its own imports
    function-local for exactly that reason; this pins the same discipline one
    level up, where it was being undone.

    A fresh interpreter, because the rest of the suite has long since imported
    everything.
    """
    code = (
        "import sys, fpl_buddy.cli; "
        "print(any(m.split('.')[0] in {'langchain', 'langchain_core', 'langchain_openai', "
        "'deepagents'} for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"
