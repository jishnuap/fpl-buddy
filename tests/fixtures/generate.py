"""Regenerate the test fixtures: python tests/fixtures/generate.py

Committed so the fixtures are reproducible rather than mystery JSON.

Element ids follow team*100 + element_type*10 + index, so 240 is Man City's first
forward. Surnames repeat across clubs on purpose -- the Solio join has to lean on
the club code, and the fixtures should punish it if it doesn't.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

OUT = Path(__file__).parent
OUT.mkdir(parents=True, exist_ok=True)

TEAMS = [
    (1, "Arsenal", "ARS"),
    (2, "Man City", "MCI"),
    (3, "Liverpool", "LIV"),
    (4, "Tottenham", "TOT"),
    (5, "Chelsea", "CHE"),
    (6, "Newcastle", "NEW"),
]

# element_type -> (count per club, base price in tenths)
SHAPE = {1: (2, 45), 2: (5, 45), 3: (5, 55), 4: (3, 65)}
POSITION_NAME = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
SURNAMES = [
    "Abbott", "Barlow", "Corrigan", "Dunmore", "Eastwood", "Fenwick", "Gallagher",
    "Hollis", "Ingram", "Jarvis", "Kelsall", "Lomax", "Marchant", "Norbury", "Oakley",
]


def build_players():
    rows = []
    for team_id, _name, short in TEAMS:
        n = 0
        for element_type, (count, base) in SHAPE.items():
            for i in range(count):
                element_id = team_id * 100 + element_type * 10 + i
                surname = SURNAMES[n % len(SURNAMES)]
                n += 1
                rows.append(
                    {
                        "id": element_id,
                        "web_name": surname,
                        "first_name": f"{short.title()}{element_type}{i}",
                        "second_name": surname,
                        "team": team_id,
                        "team_code": team_id * 7,
                        "element_type": element_type,
                        "now_cost": base + i * 5,
                        "total_points": 40 - i * 3 + element_type,
                        "form": round(3.5 - i * 0.4, 1),
                        "points_per_game": round(4.2 - i * 0.3, 1),
                        "selected_by_percent": round(20.0 - i * 2.5, 1),
                        "minutes": 900 - i * 60,
                        "status": "a",
                        "chance_of_playing_next_round": None,
                        "news": "",
                        "cost_change_event": 0,
                        "ep_next": "4.1",
                        # Underlying numbers. Attackers get real xG, defenders
                        # get xGC, so position-specific rendering has something
                        # to show. Strings and floats are mixed on purpose:
                        # that's what the live API does.
                        "expected_goals_per_90": 0.45 if element_type == 4 else 0.08,
                        "expected_assists_per_90": 0.22 if element_type == 3 else 0.05,
                        "expected_goal_involvements_per_90": (
                            0.67 if element_type == 4 else 0.27 if element_type == 3 else 0.13
                        ),
                        "expected_goals_conceded_per_90": 1.15 if element_type <= 2 else 1.4,
                        "starts": 30 - i * 4,
                        "starts_per_90": round(max(0.2, 1.0 - i * 0.15), 2),
                        "defensive_contribution": 40 if element_type <= 2 else 12,
                        "defensive_contribution_per_90": 1.9 if element_type <= 2 else 0.4,
                        "transfers_in_event": 12_000 - i * 900,
                        "transfers_out_event": 3_000 + i * 400,
                        "ict_index": str(round(60.0 - i * 5.5, 1)),
                        # Set-piece duty: first-choice taker per club per type.
                        "penalties_order": 1 if (element_type == 4 and i == 0) else None,
                        "direct_freekicks_order": 1 if (element_type == 3 and i == 0) else None,
                        "corners_and_indirect_freekicks_order": (
                            1 if (element_type == 3 and i == 1) else None
                        ),
                        # Real fields the model doesn't declare, to prove extras are ignored.
                        "goals_scored": 3,
                        "assists": 2,
                        "clean_sheets": 4,
                    }
                )
    return rows


def apply_overrides(rows):
    by_id = {r["id"]: r for r in rows}

    # A premium captaincy candidate with a name unique across the whole set.
    by_id[240].update(
        web_name="Vasquez", second_name="Vasquez", now_cost=145, form=7.8,
        total_points=96, points_per_game=8.1, selected_by_percent=71.4,
    )
    # Cheap, clearly-available midfield target. Newcastle already has an "Abbott"
    # keeper (610), so this doubles as a same-club surname collision.
    by_id[630].update(web_name="Abbott", second_name="Abbott", now_cost=50, form=4.1)
    # Hard-unavailable: validate() must refuse this outright as a transfer target.
    by_id[640].update(
        status="u", chance_of_playing_next_round=0,
        news="Has joined a club outside the Premier League",
    )
    # Doubtful: non-fatal flag only.
    by_id[641].update(
        status="d", chance_of_playing_next_round=50, news="Knock - 50% chance of playing",
    )
    # An injured player inside the squad, for the flagged-captain warning.
    by_id[320].update(status="i", chance_of_playing_next_round=25, news="Hamstring injury")
    # Deliberately unaffordable, for the over-budget test.
    by_id[642].update(now_cost=130)
    # Nulls and blanks in every numeric the API can leave empty for a player
    # with no minutes. The parser has to survive this: one null in a 558-player
    # payload must not cost a gameweek.
    by_id[611].update(
        ep_next=None, form=None, points_per_game=None, selected_by_percent=None,
        expected_goals_per_90=None, expected_assists_per_90=None,
        expected_goal_involvements_per_90=None, expected_goals_conceded_per_90=None,
        starts_per_90=None, defensive_contribution_per_90=None, ict_index="",
    )
    return rows


PLAYERS = apply_overrides(build_players())
BY_ID = {p["id"]: p for p in PLAYERS}


def events():
    # deadline_time values are placeholders: conftest rewrites them relative to
    # now so the suite never rots. The shape stays faithful to the API.
    def event(i, day, **flags):
        base = {
            "id": i, "name": f"Gameweek {i}", "deadline_time": day,
            "finished": False, "is_previous": False, "is_current": False, "is_next": False,
            "average_entry_score": 0, "highest_score": None,
        }
        base.update(flags)
        return base

    return [
        event(1, "2025-08-15T17:30:00Z", finished=True, average_entry_score=57, highest_score=121),
        event(2, "2025-08-22T17:30:00Z", finished=True, is_previous=True, average_entry_score=49),
        event(3, "2025-08-29T17:30:00Z", is_current=True),
        event(4, "2025-09-13T10:00:00Z", is_next=True),
        event(5, "2025-09-20T10:00:00Z"),
    ]


def teams():
    return [
        {
            "id": team_id, "name": name, "short_name": short,
            "strength": 4,
            "strength_overall_home": 1300 + team_id,
            "strength_overall_away": 1280 + team_id,
            "strength_attack_home": 1290 + team_id,
            "strength_attack_away": 1260 + team_id,
            "strength_defence_home": 1270 + team_id,
            "strength_defence_away": 1240 + team_id,
            "form": None,  # null in the live API; must not break parsing
            "position": team_id,
            "played": 3,
            "points": 9 - team_id,
        }
        for team_id, name, short in TEAMS
    ]


def bootstrap():
    return {
        "events": events(),
        "teams": teams(),
        "elements": PLAYERS,
        "element_types": [
            {"id": t, "singular_name_short": n} for t, n in POSITION_NAME.items()
        ],
        "total_players": 11_000_000,
    }


# 15 players: 3 per club across 5 clubs, shape 2/5/5/3, XI is 1-3-4-3.
SQUAD = [
    (110, 1),   # ARS GKP -- starting keeper
    (120, 2),   # ARS DEF
    (220, 3),   # MCI DEF
    (320, 4),   # LIV DEF (injured, for flag tests)
    (130, 5),   # ARS MID
    (230, 6),   # MCI MID
    (330, 7),   # LIV MID
    (430, 8),   # TOT MID
    (240, 9),   # MCI FWD -- captain
    (340, 10),  # LIV FWD
    (440, 11),  # TOT FWD
    (510, 12),  # CHE GKP -- reserve keeper, must be slot 12
    (420, 13),  # TOT DEF
    (520, 14),  # CHE DEF
    (530, 15),  # CHE MID
]


def my_team():
    picks = []
    for element_id, position in SQUAD:
        now = BY_ID[element_id]["now_cost"]
        # Selling price differs from now_cost on two picks (the 50%-of-rise
        # sell-on rule) so tests catch anyone reaching for now_cost instead.
        selling = now - 3 if element_id in (240, 330) else now
        picks.append(
            {
                "element": element_id,
                "position": position,
                "selling_price": selling,
                "purchase_price": now - 5,
                "multiplier": 2 if element_id == 240 else (1 if position <= 11 else 0),
                "is_captain": element_id == 240,
                "is_vice_captain": element_id == 130,
            }
        )
    return {
        "picks": picks,
        "chips": [
            {"status_for_entry": "available", "name": "wildcard", "number": 1,
             "start_event": 1, "stop_event": 19, "chip_type": "transfer"},
            {"status_for_entry": "available", "name": "3xc", "number": 1,
             "start_event": 1, "stop_event": 38, "chip_type": "team"},
            {"status_for_entry": "played", "name": "bboost", "number": 1,
             "start_event": 1, "stop_event": 38, "chip_type": "team"},
        ],
        "transfers": {
            "cost": 4, "status": "cost", "limit": 1, "made": 2,
            "bank": 15, "value": 1004,
        },
    }


# (element id the row should resolve to | None, name override, team override)
SOLIO_ROWS = [
    (240, None, None),            # exact name match
    (333, None, None),            # exact match, unowned transfer target
    (630, None, None),            # "Abbott" MID at NEW -- NEW also has an Abbott GKP (610)
    (110, None, None),            # "Abbott" GKP at ARS -- same surname, different club
    (433, "Kelsal", None),        # misspelling; fuzzy match should still land it
    (None, "Zoltan Nevermatch", None),  # no such player anywhere
    (None, None, "XYZ"),          # club code FPL doesn't know -- must not be guessed
]


def solio():
    rows = []
    expected = []
    for element_id, name_override, team_override in SOLIO_ROWS:
        source = BY_ID[element_id] if element_id else BY_ID[133]
        club = next(t for t in TEAMS if t[0] == source["team"])
        name = name_override or source["web_name"]
        team_code = team_override or club[2]
        pr = round(4.0 + source["form"] / 2, 2)
        rows.append(
            {
                "name": name,
                "team": team_code,
                "position": POSITION_NAME[source["element_type"]],
                "price": source["now_cost"],
                "ownership": source["selected_by_percent"],
                "prPoints": pr,
                "prGoals": 0.42,
                "prAssists": 0.21,
                "prBonusPoints": 0.8,
                "captainProjPoints": round(pr * 1.9, 2),
                "leverage": 1.4,
                "opponents": [{"opponent": "BOU", "isHome": True}],
            }
        )
        expected.append({"name": name, "team": team_code, "element_id": element_id})

    snapshot = {
        "generatedAt": "2025-09-12T06:00:00.000Z",
        "gameweek": 4,
        "deadlineIso": "2025-09-13T10:00:00Z",
        "source": "solio-analytics",
        "topProjected": rows,
        "topCaptains": rows[:3],
        "topDifferentials": rows[2:5],
        "topGoals": rows[:2],
        "topAssists": rows[1:3],
        "topBonus": rows[:2],
        "topDefCon": [rows[3]],
        "bestCleanSheets": [rows[3]],
        "bestAttackingFixtures": rows[:2],
        "topTransfersIn": rows[:2],
        "topTransfersOut": [rows[4]],
    }
    return snapshot, expected


# (element_id, {gameweek: predicted points}). Values are arbitrary but fixed --
# tests assert on them. 620 is deliberately missing GW6: AIrsenal emits nothing
# for a blank, and "absent" has to stay distinguishable from "predicted zero".
AIRSENAL_ROWS = [
    (240, {3: 7.4, 4: 6.81, 5: 5.02, 6: 6.11}),
    (130, {3: 4.9, 4: 5.44, 5: 4.87, 6: 3.9}),
    (630, {3: 3.1, 4: 5.2, 5: 5.4, 6: 4.8}),
    (330, {3: 4.2, 4: 3.31, 5: 3.9, 6: 4.4}),
    (320, {3: 3.8, 4: 1.02, 5: 1.1, 6: 2.9}),
    (110, {3: 3.9, 4: 4.12, 5: 3.88, 6: 4.01}),
    (433, {3: 2.9, 4: 4.6, 5: 4.9, 6: 5.2}),
    (620, {4: 4.4, 5: 3.9}),
]


def airsenal():
    """What the sidecar container writes to the shared volume.

    Names, clubs and positions come from the same player table as everything
    else, so the artefact cannot describe a player the bootstrap does not have.
    GW3 is included on purpose: it is already played by GW4, and the reader has
    to slice it off rather than sum it into a horizon total.
    """
    players = []
    for element_id, points in AIRSENAL_ROWS:
        source = BY_ID[element_id]
        club = next(t for t in TEAMS if t[0] == source["team"])
        players.append(
            {
                "element_id": element_id,
                "name": source["web_name"],
                "team": club[2],
                "position": POSITION_NAME[source["element_type"]],
                "points": {str(gw): pts for gw, pts in sorted(points.items())},
            }
        )
    players.sort(key=lambda p: -sum(p["points"].values()))

    return {
        "schema_version": 1,
        # Rewritten relative to now by tests/conftest.py: the reader drops
        # anything older than AIRSENAL_MAX_AGE_HOURS, so a fixed timestamp here
        # would turn a passing suite into a failing one after 36 hours.
        "generated_at": "2026-01-01T04:00:00+00:00",
        "airsenal_version": "1.15.0",
        "season": "2627",
        "prediction_tag": "AIrsenal_2627_testtag",
        "gameweeks": [3, 4, 5, 6],
        "players": players,
        "transfer_plan": {
            "timestamp": "2026-01-01T04:31:02",
            "points_gain": 4.31,
            "chip_played": None,
            "squad_source": "public_api_last_published",
            "moves": [{"gameweek": 4, "in": [630], "out": [320]}],
        },
        # Never carries an id. A player AIrsenal could not map to an FPL element
        # must be unproposable, not guessed at.
        "unmatched": ["Ghost Player (AIrsenal player_id 9999, no fpl_api_id)"],
    }


def fixtures():
    # GW4: three matches among the six clubs.
    pairs = [(1, 2), (3, 4), (5, 6)]
    return [
        {
            "id": 30 + i, "event": 4, "team_h": h, "team_a": a,
            "team_h_difficulty": 2 + i, "team_a_difficulty": 4 - i,
            "kickoff_time": "2025-09-13T14:00:00Z", "finished": False,
            "started": False, "minutes": 0,
        }
        for i, (h, a) in enumerate(pairs)
    ]


# Rotated pairings per gameweek, so each club's fixture run differs and a test
# can tell a real horizon from the current gameweek repeated.
FUTURE_PAIRINGS = {
    4: [(1, 2), (3, 4), (5, 6)],
    5: [(2, 3), (4, 5), (6, 1)],
    6: [(1, 3), (2, 5), (4, 6)],
    7: [(3, 1), (5, 2), (6, 4)],
    8: [(1, 4), (2, 6), (3, 5)],
}


def future_fixtures():
    """What ``/fixtures/?future=1`` returns: every unplayed fixture, all gameweeks."""
    first_kickoff = datetime(2025, 9, 13, 14, 0, tzinfo=UTC)
    rows = []
    fixture_id = 30
    for event, pairs in FUTURE_PAIRINGS.items():
        kickoff = first_kickoff + timedelta(days=7 * (event - 4))
        for i, (h, a) in enumerate(pairs):
            rows.append(
                {
                    "id": fixture_id, "event": event, "team_h": h, "team_a": a,
                    "team_h_difficulty": 2 + i, "team_a_difficulty": 4 - i,
                    "kickoff_time": kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "finished": False, "started": False, "minutes": 0,
                }
            )
            fixture_id += 1
    return rows


snapshot, expected_join = solio()
for name, payload in (
    ("bootstrap-static.json", bootstrap()),
    ("my-team.json", my_team()),
    ("fixtures.json", fixtures()),
    ("fixtures-future.json", future_fixtures()),
    ("solio-latest.json", snapshot),
    ("solio-expected-join.json", expected_join),
    ("airsenal-predictions.json", airsenal()),
):
    (OUT / name).write_text(json.dumps(payload, indent=2) + "\n")
    print("wrote", name)

# Sanity: the squad must actually be legal, or every test built on it is a lie.
counts, clubs = {}, {}
for element_id, _ in SQUAD:
    p = BY_ID[element_id]
    counts[p["element_type"]] = counts.get(p["element_type"], 0) + 1
    clubs[p["team"]] = clubs.get(p["team"], 0) + 1
assert counts == {1: 2, 2: 5, 3: 5, 4: 3}, counts
assert max(clubs.values()) <= 3, clubs
xi = [BY_ID[e]["element_type"] for e, pos in SQUAD if pos <= 11]
assert xi.count(1) == 1, xi
assert 3 <= xi.count(2) <= 5 and 2 <= xi.count(3) <= 5 and 1 <= xi.count(4) <= 3, xi
assert BY_ID[SQUAD[11][0]]["element_type"] == 1, "slot 12 must be the reserve keeper"
assert len({p["id"] for p in PLAYERS}) == len(PLAYERS), "duplicate element ids"
print("fixture squad is legal:", counts, clubs)
