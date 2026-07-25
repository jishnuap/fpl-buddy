"""Regenerate the test fixtures: python tests/fixtures/generate.py

Committed so the fixtures are reproducible rather than mystery JSON.

Element ids follow team*100 + element_type*10 + index, so 240 is Man City's first
forward. Surnames repeat across clubs on purpose -- the Solio join has to lean on
the club code, and the fixtures should punish it if it doesn't.
"""

import json
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
                        # Real fields the model doesn't declare, to prove extras are ignored.
                        "goals_scored": 3,
                        "assists": 2,
                        "clean_sheets": 4,
                        "ep_next": "4.1",
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
            "strength_attack_home": 1290, "strength_defence_home": 1270,
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


snapshot, expected_join = solio()
for name, payload in (
    ("bootstrap-static.json", bootstrap()),
    ("my-team.json", my_team()),
    ("fixtures.json", fixtures()),
    ("solio-latest.json", snapshot),
    ("solio-expected-join.json", expected_join),
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
