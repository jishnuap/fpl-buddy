# fpl-buddy

[![CI](https://github.com/jishnuap/fpl-buddy/actions/workflows/ci.yml/badge.svg)](https://github.com/jishnuap/fpl-buddy/actions/workflows/ci.yml)

A LangChain deep agent that manages a Fantasy Premier League team on a
propose-then-commit loop: it drafts a plan well before the deadline, waits for a
human, and — if nobody touches it — submits shortly before the deadline.

```
T-36h   build a factual brief -> agent proposes -> deterministic guardrails -> notify
        (captain, vice, transfers, XI/bench, chip)
T-45m   still untouched? rebuild the context from scratch, re-validate, submit
```

The agent never calls a write endpoint. It emits a structured `AgentProposal`;
non-LLM code in [`decisions/validate.py`](src/fpl_buddy/decisions/validate.py)
checks it against live data, and [`decisions/executor.py`](src/fpl_buddy/decisions/executor.py)
does the POSTs. A hallucinated element id cannot reach the FPL API.

## Quick start

```bash
make setup                      # .venv + editable install with dev extras
cp .env.example .env            # then fill in FPL_ENTRY_ID and auth
.venv/bin/fpl-buddy verify      # is the session good?
.venv/bin/fpl-buddy context     # print the brief the agent would see
.venv/bin/fpl-buddy propose     # run the agent, validate, store, notify
.venv/bin/fpl-buddy show        # latest proposal
.venv/bin/fpl-buddy commit      # execute it now (respects DRY_RUN)
```

`DRY_RUN=true` is the default and no POST leaves the process until you set it to
`false` explicitly. Run the server with:

```bash
.venv/bin/python -m fpl_buddy.main
```

## Authentication

Two paths, tried in order:

1. **Login** — `FPL_EMAIL` + `FPL_PASSWORD`. Works from a residential IP.
2. **Pasted cookies** — `FPL_COOKIE_HEADER`. Premier League's bot protection
   routinely returns `403` to datacenter IPs, which is exactly what a Container
   App is, so **treat this as the likely production path.** Open
   fantasy.premierleague.com, DevTools → Network → any `/api/me/` request →
   Request Headers → copy the whole `cookie` value.

Cookies are cached under `STATE_DIR` so restarts don't re-login.

## Safety model

- Re-validate at execution time against a **freshly built** context, never the
  stored one. Prices and injury flags move in 36 hours.
- No POST inside `EXECUTION_CUTOFF` (2 min) of the deadline — a request that
  lands late leaves you unable to tell whether it applied.
- `MAX_POINTS_HIT=0` by default: never takes a hit unless you raise it.
- Every guardrail has a test in [`tests/test_validate.py`](tests/test_validate.py).

> ⚠️ **Before setting `DRY_RUN=false`,** do one transfer manually in the FPL web
> app with DevTools open and diff the real request against `submit_transfers` /
> `submit_picks`. The write payloads were assembled from community sources, not
> an official spec.

## Layout

```
src/fpl_buddy/
  config.py          pydantic-settings; every knob is env-driven
  fpl/               auth (login + cookie fallback), client, typed models
  data/              Solio projections + the DecisionContext brief
  decisions/         schema, guardrails, store, executor
  agent/             Azure OpenAI + deepagents, prompts, read-only tools
  orchestrator.py    propose / approve / reject / amend / auto-commit
  api.py             FastAPI + signed one-click approval links
  scheduler.py       APScheduler, anchored to the real FPL deadline
  notify.py          log / smtp / webhook
  cli.py             typer entrypoints
```

See [docs/](docs/) for deployment and the decision log.

## Tests

```bash
make test
```

No network required — `tests/fixtures/` ships a trimmed `bootstrap-static`,
`my-team`, fixture list and Solio snapshot, and client tests are `respx`-mocked.
Regenerate the fixtures with `python tests/fixtures/generate.py`.

`make check` runs ruff and pytest; `make typecheck` runs mypy.

## Contributing to this repo

`main` is protected: changes go through a pull request and CI has to be green
(ruff, mypy, pytest on 3.11/3.12/3.13, and a Docker image build). Force pushes
and branch deletion are blocked, and history is kept linear — merge with squash
or rebase.

```bash
git switch -c my-change
make check
gh pr create --fill
```
