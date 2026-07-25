"""Read-only tools for the agent.

There is deliberately no tool here that writes anything. The agent cannot
transfer, cannot set a captain, cannot POST. It looks things up and returns a
structured proposal; ``decisions/`` does the rest. If you ever add a tool to
this module, it must be a getter -- that invariant is the whole safety story.

Tools return plain text rather than JSON: the model reads it more reliably, and
a truncated string degrades better than a truncated object.
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, tool

from ..data.context import DecisionContext
from ..data.solio import LEADERBOARD_KEYS
from ..fpl.client import FPLClient

logger = logging.getLogger(__name__)

MAX_ROWS = 25


def build_tools(context: DecisionContext, client: FPLClient) -> list[BaseTool]:
    """Bind the read-only toolset to one gameweek's context."""
    bootstrap = context.bootstrap

    def _describe(element_id: int) -> str:
        player = bootstrap.player(element_id)
        if player is None:
            return f"id {element_id}: unknown"
        club = bootstrap.team(player.team)
        flag = ""
        if player.is_flagged:
            chance = player.chance_of_playing_next_round
            flag = f" [{player.status}{f'/{chance}%' if chance is not None else ''}]"
        return (
            f"id={player.id} {player.web_name} ({club.short_name if club else '?'}, "
            f"{player.position}) £{player.price:.1f}m form {player.form} "
            f"pts {player.total_points} ppg {player.points_per_game} "
            f"owned {player.selected_by_percent}%{flag}"
            + (f" news: {player.news}" if player.news else "")
        )

    @tool
    def inspect_squad() -> str:
        """Show the current 15-player squad: prices, selling prices, flags, roles.

        This is the same table as in the brief. Use it to re-check a detail
        rather than trusting your memory of it.
        """
        return context.squad_table()

    @tool
    def find_player(name: str) -> str:
        """Look up FPL players by name (partial matches allowed).

        Returns element ids, clubs, positions, prices and availability. Use this
        to get an id before proposing a transfer -- never guess an id.
        """
        needle = name.strip().casefold()
        if not needle:
            return "Give a name to search for."
        hits = [
            p
            for p in bootstrap.players
            if needle in p.web_name.casefold() or needle in p.full_name.casefold()
        ]
        if not hits:
            return f"No player matching '{name}'."
        hits.sort(key=lambda p: -p.total_points)
        lines = [_describe(p.id) for p in hits[:MAX_ROWS]]
        if len(hits) > MAX_ROWS:
            lines.append(f"... and {len(hits) - MAX_ROWS} more; narrow the search.")
        return "\n".join(lines)

    @tool
    def player_detail(element_id: int) -> str:
        """Recent match-by-match history and upcoming fixtures for one player.

        Use this on captaincy candidates and transfer targets to check minutes
        and whether the returns are real or one lucky haul.
        """
        player = bootstrap.player(element_id)
        if player is None:
            return f"Element {element_id} does not exist. Use find_player to get a real id."

        try:
            data = client.player_summary(element_id)
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the run
            logger.warning("player_summary(%s) failed: %s", element_id, exc)
            return f"{_describe(element_id)}\n(Could not fetch match history: {exc})"

        lines = [_describe(element_id), "", "Recent matches (most recent last):"]
        for row in (data.get("history") or [])[-6:]:
            opponent = bootstrap.team(row.get("opponent_team", 0))
            lines.append(
                f"  GW{row.get('round')} v {opponent.short_name if opponent else '?'} "
                f"({'H' if row.get('was_home') else 'A'}): {row.get('total_points')} pts, "
                f"{row.get('minutes')} min, {row.get('goals_scored')}G "
                f"{row.get('assists')}A, bonus {row.get('bonus')}, xGI "
                f"{row.get('expected_goal_involvements', '-')}"
            )

        upcoming = (data.get("fixtures") or [])[:5]
        if upcoming:
            lines += ["", "Upcoming:"]
            for row in upcoming:
                is_home = row.get("is_home")
                opponent_id = row.get("team_a") if is_home else row.get("team_h")
                opponent = bootstrap.team(opponent_id or 0)
                lines.append(
                    f"  GW{row.get('event')} v {opponent.short_name if opponent else '?'} "
                    f"({'H' if is_home else 'A'}) FDR {row.get('difficulty')}"
                )
        return "\n".join(lines)

    @tool
    def gameweek_fixtures() -> str:
        """This gameweek's fixtures with difficulty ratings for both sides."""
        lines = context.fixture_lines(limit=20)
        return "\n".join(lines) if lines else "No fixtures found for this gameweek."

    @tool
    def club_fixtures(club: str) -> str:
        """Upcoming fixtures for one club, by name or three-letter code (e.g. 'MCI').

        Use this when weighing whether a transfer holds up beyond this week.
        """
        needle = club.strip().casefold()
        team = next(
            (
                t
                for t in bootstrap.teams
                if needle in (t.short_name.casefold(), t.name.casefold())
            ),
            None,
        )
        if team is None:
            known = ", ".join(sorted(t.short_name for t in bootstrap.teams))
            return f"Unknown club '{club}'. Known codes: {known}."

        rows = [f for f in context.fixtures if team.id in (f.team_h, f.team_a)]
        if not rows:
            return f"No fixtures for {team.name} in the loaded set (this gameweek only)."
        out = []
        for fixture in rows:
            home = fixture.team_h == team.id
            other = bootstrap.team(fixture.team_a if home else fixture.team_h)
            difficulty = fixture.team_h_difficulty if home else fixture.team_a_difficulty
            out.append(
                f"  GW{fixture.event} v {other.short_name if other else '?'} "
                f"({'H' if home else 'A'}) FDR {difficulty}"
            )
        return f"{team.name}:\n" + "\n".join(out)

    @tool
    def projections(board: str = "topProjected", limit: int = 15) -> str:
        """Read a Solio Analytics leaderboard for this gameweek.

        Boards: topProjected, topCaptains, topDifferentials, topGoals,
        topAssists, topBonus, topDefCon, bestCleanSheets,
        bestAttackingFixtures, topTransfersIn, topTransfersOut.

        These are ranked shortlists, not a full player table, and rows without an
        `id=` could not be matched to an FPL player -- never propose those as
        transfer targets.
        """
        if context.solio is None:
            return "Solio projections were unavailable for this run; rely on the brief."
        if board not in LEADERBOARD_KEYS:
            return f"Unknown board '{board}'. Valid boards: {', '.join(LEADERBOARD_KEYS)}."
        rows = context.solio.board(board, min(limit, MAX_ROWS))
        if not rows:
            return f"Board '{board}' is empty in this snapshot."
        return f"## {board}\n" + "\n".join(
            f"  {i + 1}. {row.summary()}" for i, row in enumerate(rows)
        )

    @tool
    def candidates(position: str, max_price: float = 15.0, limit: int = 12) -> str:
        """Available players in a position at or below a price, best form first.

        `position` is GKP, DEF, MID or FWD; `max_price` is in millions (e.g. 7.5).
        Injured and unavailable players are filtered out. Cross-check the
        affordability against the selling price of whoever you would drop.
        """
        wanted = position.strip().upper()
        if wanted not in {"GKP", "DEF", "MID", "FWD"}:
            return "Position must be one of GKP, DEF, MID, FWD."
        cap = round(max_price * 10)
        pool = [
            p
            for p in bootstrap.players
            if p.position == wanted and p.now_cost <= cap and not p.is_flagged
        ]
        if not pool:
            return f"No available {wanted} at £{max_price:.1f}m or below."
        pool.sort(key=lambda p: (-p.form, -p.total_points))
        return "\n".join(_describe(p.id) for p in pool[: min(limit, MAX_ROWS)])

    @tool
    def squad_rules() -> str:
        """The constraints your proposal will be checked against before submission."""
        return (
            "Squad: exactly 15 players -- 2 GKP, 5 DEF, 5 MID, 3 FWD.\n"
            "Clubs: at most 3 players from any single club.\n"
            "Starting XI: 11 players -- exactly 1 GKP, 3-5 DEF, 2-5 MID, 1-3 FWD.\n"
            "Bench: 4 players, reserve goalkeeper first, then auto-sub priority.\n"
            "Captain must be in the starting XI; vice must differ from captain.\n"
            f"Budget: bank £{context.my_team.bank_millions:.1f}m plus the selling prices "
            "of players you sell.\n"
            f"Free transfers: {context.my_team.free_transfers}. Each extra transfer "
            "costs 4 points.\n"
            f"Chips available: {', '.join(context.my_team.chips_available) or 'none'}.\n"
            "Every element id must be verified with a tool. A wrong id is a wrong player."
        )

    return [
        inspect_squad,
        squad_rules,
        find_player,
        player_detail,
        candidates,
        gameweek_fixtures,
        club_fixtures,
        projections,
    ]
