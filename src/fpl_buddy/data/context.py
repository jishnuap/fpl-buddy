"""Assemble everything the agent needs into one deterministic brief.

The agent gets a pre-built, factual snapshot rather than having to discover it
tool call by tool call. Fewer round trips, and -- more importantly -- the same
snapshot is what the guardrails validate against later, so the agent cannot
reason about a squad state that never existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import Settings
from ..fpl.client import FPLClient
from ..fpl.models import Bootstrap, Fixture, Gameweek, MyTeam
from .solio import SolioClient, SolioSnapshot, join_to_elements

logger = logging.getLogger(__name__)


@dataclass
class DecisionContext:
    gameweek: Gameweek
    my_team: MyTeam
    bootstrap: Bootstrap
    fixtures: list[Fixture]
    solio: SolioSnapshot | None
    solio_unmatched: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def hours_to_deadline(self) -> float:
        delta = self.gameweek.deadline_time - datetime.now(UTC)
        return delta.total_seconds() / 3600

    # ------------------------------------------------------------------ render
    def squad_table(self) -> str:
        lines = [
            "pos | player                    | club | £sell | £now | status | form | proj  | role"
        ]
        lines.append("-" * 100)
        for pick in sorted(self.my_team.picks, key=lambda p: p.position):
            player = self.bootstrap.player(pick.element)
            if player is None:
                lines.append(f"{pick.position:>3} | <unknown element {pick.element}>")
                continue
            club = self.bootstrap.team(player.team)
            proj = self._proj(pick.element)
            role = (
                "(C)"
                if pick.is_captain
                else "(V)"
                if pick.is_vice_captain
                else ("XI" if pick.is_starter else "BENCH")
            )
            status = "OK"
            if player.is_flagged:
                chance = player.chance_of_playing_next_round
                status = f"{player.status.upper()}{f'/{chance}%' if chance is not None else ''}"
            lines.append(
                f"{pick.position:>3} | {player.web_name:<25} | {club.short_name if club else '???':<4} "
                f"| {pick.selling_price / 10:>5.1f} | {player.price:>4.1f} | {status:<6} "
                f"| {player.form:>4.1f} | {proj:>5} | {role} [{player.position}, id={player.id}]"
            )
        return "\n".join(lines)

    def _proj(self, element_id: int) -> str:
        if self.solio is None:
            return "-"
        row = self.solio.projection_for(element_id)
        return f"{row.pr_points:.2f}" if row and row.pr_points is not None else "-"

    def news_lines(self) -> list[str]:
        out = []
        for pick in self.my_team.picks:
            player = self.bootstrap.player(pick.element)
            if player and player.news:
                out.append(f"  - {player.web_name}: {player.news}")
        return out

    def fixture_lines(self, limit: int = 12) -> list[str]:
        out = []
        for fixture in self.fixtures[:limit]:
            home = self.bootstrap.team(fixture.team_h)
            away = self.bootstrap.team(fixture.team_a)
            out.append(
                f"  {home.short_name if home else '?'} (FDR {fixture.team_h_difficulty}) v "
                f"{away.short_name if away else '?'} (FDR {fixture.team_a_difficulty})"
            )
        return out

    def render(self) -> str:
        parts = [
            f"# FPL decision brief -- {self.gameweek.name}",
            f"Deadline: {self.gameweek.deadline_time.isoformat()} "
            f"({self.hours_to_deadline:.1f}h away)",
            f"Bank: £{self.my_team.bank_millions:.1f}m | "
            f"Squad value: £{self.my_team.total_budget / 10:.1f}m | "
            f"Free transfers: {self.my_team.free_transfers}",
            f"Chips available: {', '.join(self.my_team.chips_available) or 'none'}"
            + (f" | ACTIVE CHIP: {self.my_team.active_chip}" if self.my_team.active_chip else ""),
            "",
            "## Your squad",
            self.squad_table(),
        ]

        news = self.news_lines()
        if news:
            parts += ["", "## Injury / availability news on your players", *news]

        parts += ["", f"## Fixtures for {self.gameweek.name}", *self.fixture_lines()]

        if self.solio is not None:
            parts += ["", self.solio.render()]
            if self.solio_unmatched:
                parts += [
                    "",
                    "NOTE: these Solio rows could not be mapped to an FPL id and must not be "
                    "used as transfer targets: " + ", ".join(self.solio_unmatched[:25]),
                ]
        else:
            parts += ["", "NOTE: Solio projections were unavailable for this run."]

        return "\n".join(parts)


def build_context(settings: Settings, client: FPLClient | None = None) -> DecisionContext:
    client = client or FPLClient(settings)
    bootstrap = client.bootstrap()

    gameweek = bootstrap.next_gameweek
    if gameweek is None:
        raise RuntimeError("No upcoming gameweek found -- season may be over.")

    my_team = client.my_team()
    fixtures = client.fixtures(event=gameweek.id)

    solio: SolioSnapshot | None = None
    unmatched: list[str] = []
    try:
        solio = SolioClient(settings).fetch()
        solio, unmatched = join_to_elements(solio, bootstrap)
        if solio.gameweek and solio.gameweek != gameweek.id:
            logger.warning(
                "Solio snapshot is for GW%s but the next gameweek is GW%s -- projections may be "
                "stale.",
                solio.gameweek,
                gameweek.id,
            )
    except Exception as exc:  # noqa: BLE001 - never let a third party block the run
        logger.warning("Solio fetch failed (%s); continuing without projections.", exc)

    return DecisionContext(
        gameweek=gameweek,
        my_team=my_team,
        bootstrap=bootstrap,
        fixtures=fixtures,
        solio=solio,
        solio_unmatched=unmatched,
    )
