# AIrsenal as a prediction sidecar

[AIrsenal](https://github.com/alan-turing-institute/AIrsenal) is the Alan Turing
Institute's FPL manager: a Bayesian team-scoreline model plus a player
goal-involvement model, feeding a squad optimiser. It is good at the thing this
project has no model for — **expected points per player per fixture, several
gameweeks out**.

This document is how it gets in without the agent stack inheriting its problems.

## The shape

```
nightly, own container        │  at the deadline, unchanged
                              │
airsenal_update_db            │  build_context()
airsenal_run_prediction       │    ├─ bootstrap-static   (authoritative)
dump_predictions.py           │    ├─ my-team            (authoritative)
      ↓                       │    ├─ Solio              (signal)
${STATE_DIR}/airsenal/        │    └─ predictions.json   (signal)  ← new
   predictions.json  ─────────┼──→ agent tools read the same file on demand
```

One artefact, written by a job that shares nothing with fpl-buddy but a volume.
fpl-buddy never imports `airsenal`, never opens its database, and never runs
`jax`.

## Why a sidecar and not a dependency

Three reasons, in order of how much they'd hurt.

**The deadline path cannot afford it.** `airsenal_run_prediction` fits a
hierarchical model and `airsenal_run_optimization` runs a genetic algorithm.
That is minutes to tens of minutes. The propose window is one hour and the
commit window is 45 minutes, and both re-derive everything from scratch. A model
fit does not go in there — but a model fit from six hours ago is fine, because
expected points over a fixture horizon do not move hour to hour. Team news does,
and team news is what the *agent* is for.

**The image.** `jax`, `jaxlib`, `numpy`, `pandas`, `deap`, plus `bpl` as a git
dependency (it is not on PyPI, so the build needs `git` and a compiler). Docker
Hub, the `*/10` tick job, and the ~0.9s idle tick in
[serverless.md](serverless.md) are all downstream of the image being small. An
idle tick that has to page in jax is not an idle tick any more.

**Blast radius.** AIrsenal creates a module-level SQLAlchemy engine at import
time, keyed on its own `AIRSENAL_HOME`. Importing it means two packages with
opinions about where state lives. Behind a file, its worst day is a stale
artefact — which the reader detects and drops.

## The trap: AIrsenal must not own squad state

This is the part worth reading twice.

Without login credentials, AIrsenal reconstructs your squad from the *public*
API — `entry/{id}/transfers/` and `entry/{id}/history/`. Both `get_bank()` and
`get_free_transfers()` try the logged-in endpoint first and, on failure, warn
and fall back to a public estimate that in AIrsenal's own words "will not
include any transfers made in the current gameweek".

So AIrsenal's view of your team is *last published picks*, which is stale from
the moment you make a transfer and until the next deadline passes.

fpl-buddy already has the authenticated `my-team` endpoint: exact bank, exact
selling prices, exact free transfers, live chips. It is right and AIrsenal is
guessing.

Therefore the split is:

| | Authority |
|---|---|
| Expected points per player per fixture | **AIrsenal** |
| Squad, bank, selling prices, free transfers, chips | **fpl-buddy (`my-team`)** |
| Element ids, prices, availability | **fpl-buddy (`bootstrap-static`)** |

The sidecar emits **predictions**. Whether it also emits AIrsenal's own transfer
plan is a flag, off by default, and when it is on the plan is rendered with its
squad provenance stamped on it. The transfer *arithmetic* — what you can afford,
what the club limit allows — stays where the authoritative numbers are.

## Credentials: none

`FPL_TEAM_ID` and nothing else. **Never set `FPL_LOGIN` / `FPL_PASSWORD` in the
sidecar's environment.** AIrsenal ships `airsenal_make_transfers` and
`airsenal_set_lineup` and will POST to FPL given credentials, and the whole
safety story of this project is that exactly one code path writes:
`decisions/executor.py`, after re-validating against a freshly built context.

`run.sh` refuses to start if either variable is set. That is cheap and it makes
the invariant enforceable rather than merely documented.

Everything the predictions need — `bootstrap-static`, fixtures, results, player
histories — is public.

## The artefact

`${STATE_DIR}/airsenal/predictions.json`, written atomically (temp file +
`os.replace`), so a reader never sees a half-written file even on a filesystem
without locking. That matters on GCS FUSE, per
[serverless.md](serverless.md#concurrent-writes).

```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-07-26T04:12:33+00:00",
  "airsenal_version": "1.15.0",
  "season": "2627",
  "prediction_tag": "AIrsenal_2627_a41f9c",
  "gameweeks": [3, 4, 5],          // horizon this run predicted
  "players": [
    {
      "element_id": 427,            // FPL element id, from Player.fpl_api_id
      "name": "Haaland",
      "team": "MCI",
      "position": "FWD",
      "points": {"3": 6.81, "4": 5.02, "5": 7.11}
    }
  ],
  "transfer_plan": null,            // or the object below, when enabled
  "unmatched": ["Some Player (AIrsenal player_id 4412, no fpl_api_id)"]
}
```

with `transfer_plan`, when `AIRSENAL_EMIT_TRANSFER_PLAN=true`:

```jsonc
{
  "timestamp": "2026-07-26T04:31:02",
  "points_gain": 4.31,
  "chip_played": null,
  "squad_source": "public_api_last_published",   // never "my-team"
  "moves": [{"gameweek": 3, "in": [123, 456], "out": [789, 12]}]
}
```

### Rules the artefact obeys

**`element_id` is the FPL element id or the row does not exist.** It comes from
`Player.fpl_api_id`, an exact integer join — none of the fuzzy name matching the
Solio integration needs. An AIrsenal player without an `fpl_api_id` goes into
`unmatched` and is never emitted with a guessed id. Same invariant as
[the Solio join](../src/fpl_buddy/data/solio.py): a wrong id here is a wrong
player transferred in.

**Points are per gameweek, not summed.** The horizon fpl-buddy cares about
(`FIXTURE_HORIZON_GAMEWEEKS`) is a different setting from the one the sidecar
ran with, and a blank gameweek is real information — a team with no fixture that
week reads as absent, not as zero. This is also why the dump script queries
`player_prediction` directly instead of calling `get_predicted_points`, which
defaults every unpredicted player to `0.0`.

**String keys in `points`.** JSON has no integer keys; the reader coerces.

## Reading it

`data/airsenal.py`, deliberately built to the same pattern as `data/solio.py`.

```python
snapshot, note = load_snapshot(settings, gameweek=gameweek.id, horizon=5)
```

`load_snapshot` returns `(None, reason)` — logging a warning, never raising —
when the file is missing, unparseable, older than `AIRSENAL_MAX_AGE_HOURS`, or
does not cover the gameweek being submitted. Every one of those is "run without
it", exactly as a Solio fetch failure is today. The reason goes into the brief,
because "no AIrsenal section" and "AIrsenal has no opinion" are different things
and the agent cannot tell them apart on its own.

**Presence, not configuration, is the switch.** There is no `AIRSENAL_ENABLED`.
The reader keys on the file existing, for the same reason `open_archive()` keys
on notes existing rather than on `KNOWLEDGE_SOURCES_FILE`: whether the job
should *produce* an artefact is a different question from whether there is one
to read, and conflating them fails silently.

**Staleness is sliced, not rejected.** A snapshot covering GW3–5 when GW4 is
next is still useful; it is used for GW4 and GW5 and the GW3 column is dropped.
A snapshot that does not contain the target gameweek at all is dropped whole.
The brief always states `generated_at` and which gameweeks are covered, because
a projection whose provenance the agent cannot see is a projection it cannot
discount.

### What lands in the brief

1. **A column in the squad table.** `ais` next to the existing `proj` (Solio).
   Two independent models side by side, per player you own.

2. **A disagreement block.** Where the two differ by at least
   `AIRSENAL_DISAGREEMENT_THRESHOLD` (default 1.5) on a squad player, the brief
   says so by name. This is the actual dividend of having two models: agreement
   is background, disagreement is where the agent should spend its thinking, and
   neither number is worth much on its own. The block says explicitly that the
   two are not the same quantity — Solio projects one gameweek, AIrsenal a
   horizon — so it reads as a pointer, not an arithmetic claim.

3. **An expected-points section** over the horizon, top
   `AIRSENAL_BRIEF_LIMIT`, tagged `[OWNED]` / `[not owned]` in exactly the
   format the Solio boards use — because the failure that produced that tagging
   (captaining a player from a league-wide board) applies identically here.

4. **The transfer plan**, if present, under a heading that states its squad
   provenance and the date it was computed.

Token cost is bounded the way the article index is: the brief carries a fixed
top-N and the tools reach the whole table.

## The tools

Three, all read-only, all returning text, all degrading to one clear sentence
when there is no snapshot — which repeats the reason from `load_snapshot`, so an
agent that reaches for the tool learns the same thing the brief said.

| Tool | Answers |
|---|---|
| `airsenal_points(element_id)` | Per-gameweek expected points for one player, the horizon total, and their rank within position |
| `airsenal_top(position, limit)` | The expected-points table beyond what the brief shows, filterable by position |
| `airsenal_transfer_plan()` | AIrsenal's own suggested moves, with the stale-squad caveat attached |

`transfer_options(element_out)` and `underlying_stats(element_id)` also gain an
AIrsenal figure. `transfer_options` keeps **ranking** on Solio — adding a second
model should not silently reshuffle a list the prompt already describes — but
the agent can now see where the models disagree about a target it is offered.

`captain_candidate_lines` uses AIrsenal only as a fallback, ahead of FPL's
`ep_next`, for the same reason.

Note what `airsenal_transfer_plan` is *not*: it is not an instruction and its
ids still have to survive validation. It is one more opinion, from a model that
cannot read a press conference, computed against a squad it may have wrong.

## Failure modes

| What breaks | What happens |
|---|---|
| Sidecar job fails | Yesterday's artefact is used until it ages out at `AIRSENAL_MAX_AGE_HOURS`, then the brief says AIrsenal was unavailable |
| Artefact stale by a gameweek | Dropped whole; brief says which gameweeks it covered |
| Artefact covers GW3–5, GW4 is next | GW3 sliced off, GW4–5 used, coverage stated in the brief |
| Corrupt / half-written JSON | `load_snapshot` returns `None`, warns; the atomic write should make this unreachable |
| Sidecar writes a newer schema | Refused with a version message rather than half-read |
| An AIrsenal player has no `fpl_api_id` | Listed in `unmatched`, never given an id, never proposable |
| Sidecar given FPL credentials | `run.sh` exits 78 before doing anything |

There is no case where a missing or bad artefact stops a proposal. That is the
same posture as Solio and the article archive, and it is not negotiable for
anything on the deadline path.

## Cost

The prediction run is nightly and measured in minutes; on Container Apps Jobs or
Cloud Run Jobs that is a rounding error against the same free grant the tick job
barely touches. The real cost is the AIrsenal database — three seasons of
history — which lives on the volume and is built once by
`airsenal_setup_initial_db`. Budget a few hundred MB and do not rebuild it per
run.

## What this does not do

It does not make the optimiser the author of the proposal. The agent still
writes the proposal and the guardrails still validate it; AIrsenal is a second
projection source that happens to be much better grounded than the first.

Promoting the optimiser's plan to a **candidate the agent must explicitly accept
or argue down**, with the deviation recorded in `AgentProposal`, is a real and
probably better design — the model does multi-week expected value properly and
the agent has the team news the model cannot see. It is a change to the proposal
schema and the prompt, and it should be made after a few gameweeks of watching
the two disagree in the brief, not before.
