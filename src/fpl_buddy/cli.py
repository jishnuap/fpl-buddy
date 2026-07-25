"""Command line interface.

This exists so the whole loop can be exercised on a Tuesday afternoon instead of
waiting for a real deadline: ``verify`` the session, print the ``context`` the
agent will see, ``propose``, then ``commit`` by hand. Same code paths the
scheduler uses, same guardrails.
"""

from __future__ import annotations

import logging
import sys
from typing import NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .approval import review_url
from .config import Settings, get_settings
from .data.context import build_context
from .decisions.executor import ExecutionBlocked
from .decisions.schema import Proposal, ProposalStatus
from .decisions.store import build_store
from .decisions.validate import validate
from .fpl.auth import FPLAuthError
from .fpl.client import FPLClient
from .orchestrator import NotActionable, Orchestrator, ProposalNotFound

app = typer.Typer(
    add_completion=False,
    help="Propose and commit FPL moves. Read-only until you turn DRY_RUN off.",
    no_args_is_help=True,
)
console = Console()


def _setup(verbose: bool = False) -> Settings:
    settings = get_settings()
    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, settings.log_level.upper(), 20),
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return settings


def _die(message: str) -> NoReturn:
    console.print(f"[bold red]✗[/] {message}")
    raise typer.Exit(code=1)


def _banner(settings: Settings) -> None:
    mode = (
        "[bold green]DRY RUN[/] -- nothing will be sent to FPL"
        if settings.dry_run
        else "[bold red]LIVE[/] -- writes will reach FPL"
    )
    console.print(f"entry {settings.fpl_entry_id or '(unset)'} | {mode}")


# --------------------------------------------------------------------- sessions


@app.command()
def login(verbose: bool = False) -> None:
    """Obtain and cache an FPL session, then prove it works."""
    settings = _setup(verbose)
    client = FPLClient(settings)
    try:
        client.auth.get_session_cookies(force_refresh=True)
    except FPLAuthError as exc:
        _die(
            f"{exc}\n\nIf login is blocked from this network, paste FPL_COOKIE_HEADER "
            "from DevTools (Network -> any /api/me/ request -> Request Headers -> cookie)."
        )
    if not client.verify_session():
        _die("Got cookies, but /api/me/ still rejects them. They may be for another account.")
    console.print("[bold green]✓[/] Session is live and cached.")


@app.command()
def verify(verbose: bool = False) -> None:
    """Check the FPL session and the entry id without changing anything."""
    settings = _setup(verbose)
    _banner(settings)
    client = FPLClient(settings)

    if not client.verify_session():
        _die("Session is not valid. Run `fpl-buddy login`, or refresh FPL_COOKIE_HEADER.")
    console.print("[bold green]✓[/] FPL session OK.")

    try:
        my_team = client.my_team()
    except Exception as exc:  # noqa: BLE001
        _die(f"Session works but /my-team/ failed: {exc}")
    console.print(
        f"[bold green]✓[/] Squad loaded: {len(my_team.picks)} players, "
        f"bank £{my_team.bank_millions:.1f}m, {my_team.free_transfers} free transfer(s), "
        f"chips: {', '.join(my_team.chips_available) or 'none'}."
    )

    gameweek = client.bootstrap().next_gameweek
    if gameweek is None:
        console.print("[yellow]![/] No upcoming gameweek -- season may be over.")
    else:
        console.print(
            f"[bold green]✓[/] Next: {gameweek.name}, deadline "
            f"{gameweek.deadline_time.astimezone().strftime('%a %d %b %H:%M %Z')}."
        )


# ------------------------------------------------------------------------ views


@app.command()
def context(verbose: bool = False) -> None:
    """Print the exact brief the agent will reason over."""
    settings = _setup(verbose)
    console.print(build_context(settings, FPLClient(settings)).render())


@app.command(name="list")
def list_proposals(verbose: bool = False) -> None:
    """List stored proposals, newest first."""
    settings = _setup(verbose)
    proposals = sorted(build_store(settings).list_all(), key=lambda p: p.created_at, reverse=True)
    if not proposals:
        console.print("No proposals stored yet.")
        return

    table = Table(show_header=True, header_style="bold")
    for column in ("id", "gw", "status", "created", "headline"):
        table.add_column(column)
    for proposal in proposals:
        table.add_row(
            proposal.id,
            str(proposal.gameweek),
            _coloured(proposal.status),
            proposal.created_at.astimezone().strftime("%d %b %H:%M"),
            proposal.headline(),
        )
    console.print(table)


@app.command()
def show(
    proposal_id: str = typer.Argument(None, help="Defaults to the latest proposal."),
    brief: bool = typer.Option(False, "--brief", help="Also print the stored context snapshot."),
    transcript: bool = typer.Option(False, "--transcript", help="Print the agent's reasoning."),
    verbose: bool = False,
) -> None:
    """Show one proposal in full."""
    settings = _setup(verbose)
    store = build_store(settings)
    proposal = store.get(proposal_id) if proposal_id else store.latest()
    if proposal is None:
        _die("No such proposal." if proposal_id else "No proposals stored yet.")

    from .notify import render_proposal

    _subject, text, _html = render_proposal(proposal, settings)
    console.print(
        Panel(
            text,
            title=f"{proposal.id} [{_coloured(proposal.status)}]",
            border_style="red" if proposal.fatal_issues else "green",
        )
    )
    if proposal.human_note:
        console.print(f"[bold]Your note:[/] {proposal.human_note}")
    if proposal.execution_error:
        console.print(f"[bold red]Execution error:[/] {proposal.execution_error}")
    if brief and proposal.context_snapshot:
        console.print(Panel(proposal.context_snapshot, title="brief", border_style="dim"))
    if transcript and proposal.agent_transcript:
        console.print(Panel(proposal.agent_transcript, title="agent", border_style="dim"))


# ------------------------------------------------------------------- the loop


@app.command()
def propose(verbose: bool = False) -> None:
    """Run the agent now and store the resulting proposal."""
    settings = _setup(verbose)
    _banner(settings)
    orchestrator = Orchestrator(settings)
    with console.status("Building the brief and running the agent..."):
        proposal = orchestrator.propose()

    show(proposal_id=proposal.id, verbose=verbose)
    console.print(f"\nReview link: {review_url(settings, proposal.id)}")
    if proposal.fatal_issues:
        console.print("[bold red]This proposal cannot be submitted.[/] Amend it or act in the app.")
        raise typer.Exit(code=2)


@app.command()
def check(
    proposal_id: str = typer.Argument(None, help="Defaults to the latest proposal."),
    verbose: bool = False,
) -> None:
    """Re-validate a stored proposal against live data, without submitting."""
    settings = _setup(verbose)
    store = build_store(settings)
    proposal = store.get(proposal_id) if proposal_id else store.latest()
    if proposal is None:
        _die("No such proposal." if proposal_id else "No proposals stored yet.")

    client = FPLClient(settings)
    fresh = build_context(settings, client)
    issues = validate(proposal.agent, fresh, settings, for_execution=True)
    if not issues:
        console.print("[bold green]✓[/] Still valid against live data; safe to submit.")
        return
    for issue in issues:
        marker = "[bold red]FATAL[/]" if issue.fatal else "[yellow]warn [/]"
        console.print(f"{marker} [{issue.code}] {issue.message}")
    if any(i.fatal for i in issues):
        raise typer.Exit(code=2)


@app.command()
def approve(
    proposal_id: str = typer.Argument(None, help="Defaults to the latest proposal."),
    note: str = typer.Option("", help="Optional note stored with the decision."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    verbose: bool = False,
) -> None:
    """Approve a proposal. Submits immediately unless EXECUTE_ON_APPROVAL is off."""
    settings = _setup(verbose)
    orchestrator = Orchestrator(settings)
    proposal = _resolve(orchestrator, proposal_id)

    console.print(Panel(proposal.headline(), title=f"GW{proposal.gameweek}"))
    if not settings.dry_run and settings.execute_on_approval and not yes:
        console.print("[bold red]DRY_RUN is off: this will really submit to FPL.[/]")
        if not typer.confirm("Submit this now?"):
            raise typer.Exit(code=1)

    try:
        updated = orchestrator.approve(proposal.id, note=note or None)
    except (NotActionable, ProposalNotFound) as exc:
        _die(str(exc))
    except ExecutionBlocked as exc:
        _die(f"Re-validation blocked submission: {exc}")
    console.print(f"[bold green]✓[/] {updated.id} is now {updated.status.value}.")


@app.command()
def reject(
    proposal_id: str = typer.Argument(None, help="Defaults to the latest proposal."),
    note: str = typer.Option("", help="Why, for your own records."),
    verbose: bool = False,
) -> None:
    """Reject a proposal so the deadline job leaves it alone."""
    settings = _setup(verbose)
    orchestrator = Orchestrator(settings)
    proposal = _resolve(orchestrator, proposal_id)
    try:
        updated = orchestrator.reject(proposal.id, note=note or None)
    except (NotActionable, ProposalNotFound) as exc:
        _die(str(exc))
    console.print(f"[bold green]✓[/] {updated.id} rejected; nothing will be submitted.")


@app.command()
def amend(
    note: str = typer.Argument(..., help="What to change, e.g. 'captain Oakley instead'."),
    proposal_id: str = typer.Option(None, help="Defaults to the latest proposal."),
    verbose: bool = False,
) -> None:
    """Send feedback and get a fresh proposal."""
    settings = _setup(verbose)
    orchestrator = Orchestrator(settings)
    proposal = _resolve(orchestrator, proposal_id)
    with console.status("Re-running the agent with your feedback..."):
        try:
            revised = orchestrator.amend(proposal.id, note)
        except (NotActionable, ProposalNotFound) as exc:
            _die(str(exc))
    show(proposal_id=revised.id, verbose=verbose)


@app.command()
def commit(verbose: bool = False) -> None:
    """Run the deadline job now: rebuild the context, re-validate, submit."""
    settings = _setup(verbose)
    _banner(settings)
    orchestrator = Orchestrator(settings)
    try:
        proposal = orchestrator.auto_commit()
    except ExecutionBlocked as exc:
        _die(f"Refused to submit: {exc}")
    if proposal is None:
        console.print("Nothing to commit.")
        return
    console.print(f"[bold green]✓[/] {proposal.id} is now {proposal.status.value}.")
    if proposal.execution_error:
        console.print(f"[bold red]![/] {proposal.execution_error}")


@app.command()
def schedule(verbose: bool = False) -> None:
    """Show when the propose and commit jobs would run for the next deadline."""
    from datetime import timedelta

    settings = _setup(verbose)
    gameweek = FPLClient(settings).bootstrap().next_gameweek
    if gameweek is None:
        _die("No upcoming gameweek.")

    deadline = gameweek.deadline_time.astimezone()
    propose_at = deadline - timedelta(hours=settings.propose_hours_before_deadline)
    commit_at = deadline - timedelta(minutes=settings.commit_minutes_before_deadline)
    fmt = "%a %d %b %H:%M %Z"
    console.print(
        f"{gameweek.name}\n"
        f"  propose   {propose_at.strftime(fmt)}\n"
        f"  commit    {commit_at.strftime(fmt)}\n"
        f"  deadline  {deadline.strftime(fmt)}\n"
        f"  auto-commit {'on' if settings.auto_commit_enabled else 'off'}, "
        f"dry_run {'on' if settings.dry_run else 'off'}"
    )


@app.command()
def serve(verbose: bool = False) -> None:
    """Run the API and scheduler in the foreground (what the container does)."""
    _setup(verbose)
    from .main import run

    run()


# --------------------------------------------------------------------------- #


def _resolve(orchestrator: Orchestrator, proposal_id: str | None) -> Proposal:
    proposal = orchestrator.get(proposal_id) if proposal_id else orchestrator.latest()
    if proposal is None:
        _die("No such proposal." if proposal_id else "No proposals stored yet.")
    return proposal


def _coloured(status: ProposalStatus) -> str:
    colour = {
        ProposalStatus.PENDING: "yellow",
        ProposalStatus.APPROVED: "green",
        ProposalStatus.EXECUTED: "bold green",
        ProposalStatus.AUTO_EXECUTED: "bold green",
        ProposalStatus.REJECTED: "dim",
        ProposalStatus.AMENDED: "cyan",
        ProposalStatus.FAILED: "bold red",
        ProposalStatus.EXPIRED: "dim",
        ProposalStatus.SUPERSEDED: "dim",
    }.get(status, "white")
    return f"[{colour}]{status.value}[/]"


def main() -> None:  # pragma: no cover - console_scripts shim
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
