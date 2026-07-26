#!/usr/bin/env python3
"""Dump AIrsenal's predictions to the JSON artefact fpl-buddy reads.

This script runs *inside the sidecar image*, next to AIrsenal and its database.
Nothing in ``src/fpl_buddy`` imports it, and it imports nothing from
``fpl_buddy`` -- the file on disk is the entire interface between the two, which
is what keeps jax out of the deadline path. See ``docs/airsenal.md``.

What it emits is deliberately narrow: expected points per player per gameweek.
AIrsenal's view of *your squad* comes from the public API and is stale the
moment you make a transfer, while fpl-buddy holds an authenticated ``my-team``.
The transfer plan is therefore opt-in and carries its own provenance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from airsenal.framework.schema import Fixture, PlayerPrediction, session
from airsenal.framework.utils import (
    CURRENT_SEASON,
    NEXT_GAMEWEEK,
    get_latest_prediction_tag,
    get_player,
)

SCHEMA_VERSION = 1


def airsenal_version() -> str:
    try:
        from importlib.metadata import version

        return version("airsenal")
    except Exception:  # noqa: BLE001 - a version string is not worth failing over
        return "unknown"


def collect_players(
    tag: str, season: str, gameweeks: list[int], dbsession
) -> tuple[list[dict], list[str]]:
    """Predicted points per player per gameweek, keyed by FPL element id.

    Queried straight off ``player_prediction`` rather than through
    ``get_predicted_points``, which fills every player in with a default of
    0.0. That default is indistinguishable from a real prediction of zero, and
    the difference matters: a player whose club has no fixture that week should
    read as *absent*, not as "predicted to score nothing".

    Two rows in one gameweek is a double gameweek, and they are summed.
    """
    rows = dbsession.execute(
        select(
            PlayerPrediction.player_id,
            Fixture.gameweek,
            PlayerPrediction.predicted_points,
        )
        .join(Fixture, PlayerPrediction.fixture_id == Fixture.fixture_id)
        .where(
            PlayerPrediction.tag == tag,
            Fixture.season == season,
            Fixture.gameweek.in_(gameweeks),
        )
    ).all()

    by_player: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for player_id, gameweek, points in rows:
        by_player[player_id][gameweek] += float(points)

    players: list[dict] = []
    unmatched: list[str] = []

    for player_id, points in by_player.items():
        player = get_player(player_id, dbsession=dbsession)
        if player is None:
            unmatched.append(f"AIrsenal player_id {player_id} (not in the player table)")
            continue

        # The whole join, and the reason this integration needs no fuzzy name
        # matching: AIrsenal stores the FPL element id. A player without one is
        # reported, never guessed -- a wrong id here is a wrong transfer.
        if player.fpl_api_id is None:
            unmatched.append(f"{player} (AIrsenal player_id {player_id}, no fpl_api_id)")
            continue

        players.append(
            {
                "element_id": int(player.fpl_api_id),
                "name": str(player),
                "team": player.team(season, gameweeks[0]) or "",
                "position": player.position(season) or "",
                # JSON has no integer keys. The reader coerces.
                "points": {str(gw): round(pts, 3) for gw, pts in sorted(points.items())},
            }
        )

    players.sort(key=lambda p: -sum(p["points"].values()))
    return players, sorted(set(unmatched))


def collect_transfer_plan(season: str, fpl_team_id: int, dbsession) -> dict | None:
    """AIrsenal's own suggested moves, translated to element ids.

    Opt-in, because it is computed against a squad AIrsenal reconstructed from
    the *public* API -- your last published picks, which is wrong as soon as you
    have transferred this week. ``squad_source`` says so in the artefact so the
    caveat survives into the brief instead of living only here.
    """
    from airsenal.scripts.get_transfer_suggestions import get_transfer_suggestions

    rows = get_transfer_suggestions(dbsession, season=season, fpl_team_id=fpl_team_id)
    if not rows:
        return None

    moves: dict[int, dict[str, list[int]]] = defaultdict(lambda: {"in": [], "out": []})
    for row in rows:
        player = get_player(row.player_id, dbsession=dbsession)
        if player is None or player.fpl_api_id is None:
            # One unresolvable id makes the whole plan unusable: a plan missing
            # a leg is not a cheaper plan, it is a different and illegal one.
            print(
                f"Transfer plan references player_id {row.player_id} with no FPL id; "
                "dropping the plan.",
                file=sys.stderr,
            )
            return None
        moves[row.gameweek]["in" if row.in_or_out == 1 else "out"].append(
            int(player.fpl_api_id)
        )

    first = rows[0]
    return {
        "timestamp": str(first.timestamp),
        "points_gain": round(float(first.points_gain), 3),
        "chip_played": first.chip_played or None,
        "squad_source": "public_api_last_published",
        "moves": [
            {"gameweek": gw, "in": legs["in"], "out": legs["out"]}
            for gw, legs in sorted(moves.items())
        ],
    }


def write_atomically(payload: dict, destination: Path) -> None:
    """Temp file in the same directory, then replace.

    The reader may be a different container on a shared volume, and Cloud
    Storage FUSE does not do file locking (see docs/serverless.md). A rename is
    the one operation that cannot hand anyone a half-written file.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle as file:
            json.dump(payload, file, indent=1, sort_keys=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(handle.name, destination)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=os.environ.get(
            "AIRSENAL_SNAPSHOT_PATH", "/data/airsenal/predictions.json"
        ),
        help="Where to write the artefact.",
    )
    parser.add_argument(
        "--weeks-ahead",
        type=int,
        default=int(os.environ.get("AIRSENAL_WEEKS_AHEAD", "5")),
        help="How many gameweeks of predictions to emit, starting from the next one.",
    )
    parser.add_argument(
        "--transfer-plan",
        action="store_true",
        default=os.environ.get("AIRSENAL_EMIT_TRANSFER_PLAN", "").lower()
        in {"1", "true", "yes"},
        help="Also emit AIrsenal's optimiser output. Off by default; see the docstring.",
    )
    parser.add_argument("--season", default=CURRENT_SEASON)
    parser.add_argument(
        "--fpl-team-id", type=int, default=int(os.environ.get("FPL_TEAM_ID", "0") or 0)
    )
    args = parser.parse_args()

    tag = get_latest_prediction_tag(season=args.season, dbsession=session)
    if not tag:
        print(
            "No prediction tag in the database -- run airsenal_run_prediction first.",
            file=sys.stderr,
        )
        return 1

    gameweeks = list(range(NEXT_GAMEWEEK, NEXT_GAMEWEEK + args.weeks_ahead))
    players, unmatched = collect_players(tag, args.season, gameweeks, session)
    if not players:
        print(
            f"No predictions found for tag {tag}, gameweeks {gameweeks}.", file=sys.stderr
        )
        return 1

    transfer_plan = None
    if args.transfer_plan:
        if not args.fpl_team_id:
            print("--transfer-plan needs FPL_TEAM_ID; skipping the plan.", file=sys.stderr)
        else:
            transfer_plan = collect_transfer_plan(args.season, args.fpl_team_id, session)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "airsenal_version": airsenal_version(),
        "season": args.season,
        "prediction_tag": tag,
        "gameweeks": gameweeks,
        "players": players,
        "transfer_plan": transfer_plan,
        "unmatched": unmatched,
    }

    destination = Path(args.out)
    write_atomically(payload, destination)
    print(
        f"Wrote {len(players)} players over GW{gameweeks[0]}-{gameweeks[-1]} "
        f"to {destination}" + (f" ({len(unmatched)} unmatched)" if unmatched else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
