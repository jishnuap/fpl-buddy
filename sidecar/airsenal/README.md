# AIrsenal prediction sidecar

A scheduled job that runs [AIrsenal](https://github.com/alan-turing-institute/AIrsenal)
and leaves one JSON file where fpl-buddy can read it. The design and the
reasoning are in [docs/airsenal.md](../../docs/airsenal.md); this is the
operating manual.

```bash
docker build -t fpl-buddy-airsenal:local sidecar/airsenal
docker run --rm -v fpl-buddy-state:/data -e FPL_TEAM_ID=1234567 fpl-buddy-airsenal:local
```

fpl-buddy picks the artefact up on its next run with no configuration, as long
as `STATE_DIR` points at the same volume. There is no enable flag: the reader
keys on the file existing.

## Environment

| | Default | |
|---|---|---|
| `FPL_TEAM_ID` | — | Your entry id. Predictions work without it; the optional transfer plan does not. |
| `AIRSENAL_HOME` | `/data/airsenal/db` | Where the AIrsenal database lives. **Must be durable.** |
| `AIRSENAL_SNAPSHOT_PATH` | `/data/airsenal/predictions.json` | Where the artefact is written. |
| `AIRSENAL_WEEKS_AHEAD` | `5` | Gameweeks of predictions to emit. Match it to `FIXTURE_HORIZON_GAMEWEEKS`. |
| `AIRSENAL_N_PREVIOUS_SEASONS` | `3` | History depth, on first build only. |
| `AIRSENAL_EMIT_TRANSFER_PLAN` | `false` | Also run the optimiser. Read the caveat below first. |
| `AIRSENAL_DB_URI` / `_USER` / `_PASSWORD` | — | Use postgres instead of a SQLite file on the volume. |

**`FPL_LOGIN` and `FPL_PASSWORD` must stay unset.** `run.sh` exits 78 if it
finds either. AIrsenal can submit transfers and set your lineup given
credentials, and the one thing this project guarantees is that only
`decisions/executor.py` writes to FPL. Nothing here needs auth — every endpoint
the predictions touch is public.

## The first run is the slow one

`airsenal_setup_initial_db` pulls three seasons of history. Expect a long first
run and a few hundred MB on the volume. Every run after that is incremental, and
if you ever see the initial build happen twice, your volume is not persisting.

Build it once, interactively, before you put it on a schedule:

```bash
docker run --rm -it -v fpl-buddy-state:/data -e FPL_TEAM_ID=1234567 \
  fpl-buddy-airsenal:local
```

## Scheduling it

Nightly is more than enough. Expected points over a fixture horizon do not move
hour to hour — team news does, and that is the agent's job, not the model's.
Anything more often is paying for a model fit to tell you the same thing.

This job knows nothing about deadlines and does not need to: run it after the
gameweek finishes and before the next deadline, which any nightly slot
satisfies.

```bash
# Azure Container Apps Jobs
az containerapp job create --name fpl-buddy-airsenal --resource-group fpl-buddy \
  --trigger-type Schedule --cron-expression "0 4 * * *" \
  --image youruser/fpl-buddy-airsenal:0.1.0 --cpu 1 --memory 2Gi \
  --env-vars FPL_TEAM_ID=1234567

# Cloud Run Jobs
gcloud run jobs create fpl-buddy-airsenal --region europe-west2 \
  --image youruser/fpl-buddy-airsenal:0.1.0 --memory 2Gi \
  --set-env-vars FPL_TEAM_ID=1234567
gcloud scheduler jobs create http fpl-buddy-airsenal-nightly --schedule "0 4 * * *" ...
```

Give it 2Gi. The model fit is the memory-hungry part, and an OOM kill mid-fit
leaves the previous artefact in place — which degrades correctly, but quietly.
Check the artefact's `generated_at` if you want to know the job is alive.

## The transfer plan caveat

`AIRSENAL_EMIT_TRANSFER_PLAN=true` also runs the squad optimiser. Understand
what it optimises against first.

Without login, AIrsenal reconstructs your squad from the public API — your last
*published* picks — and estimates bank and free transfers from your entry
history. Its own warning text says the estimate "will not include any transfers
made in the current gameweek". So the plan is computed against the team you had,
not the team you have.

fpl-buddy has the authenticated `my-team`: exact bank, exact selling prices,
exact free transfers. It is right and this is guessing. The artefact stamps
`"squad_source": "public_api_last_published"` so the caveat reaches the brief,
and the agent is told the plan is an argument rather than an instruction — but
if you have already transferred this week, expect it to be wrong.

The predictions have no such problem. They are the reason to run this.

## Checking it worked

```bash
docker run --rm -v fpl-buddy-state:/data python:3.12-slim python -c \
"import json;d=json.load(open('/data/airsenal/predictions.json'));\
print(d['generated_at'], d['gameweeks'], len(d['players']), 'unmatched:', len(d['unmatched']))"
```

Then from fpl-buddy, where a missing or stale artefact says so explicitly:

```bash
fpl-buddy context | grep -i airsenal
```
