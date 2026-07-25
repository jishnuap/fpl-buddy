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

## Running the container

Published to Docker Hub on every `v*` tag, for `linux/amd64` and `linux/arm64`:

```bash
docker run -d --name fpl-buddy --restart unless-stopped \
  -p 8080:8080 --env-file .env -v fpl-buddy-state:/data \
  youruser/fpl-buddy:0.1.0
```

Deployment is manual and host-agnostic — there is no infrastructure-as-code here.
[docs/deployment.md](docs/deployment.md) has the environment contract and the few
rules any host has to satisfy (one instance, never scaled to zero, durable
`STATE_DIR`).

## Authentication

FPL uses OAuth: a short-lived `access_token` (**8 hours**) plus a long-lived
`refresh_token` (**~180 days**), issued by PingOne and carried as cookies.

Paste the cookie header from your browser — DevTools → Network → any `/api/me/`
request → Request Headers → the whole `cookie` value — into
`FPL_COOKIE_HEADER`. Programmatic login is not viable off your own machine;
Premier League's bot protection returns `403` to datacenter IPs.

Because a gameweek cycle (propose at T-36h, commit at T-45m) outlives the access
token, **refresh is load-bearing, not an optimisation.** It happens automatically
before any request that needs it. Two consequences:

- `STATE_DIR` must be durable. Refresh tokens rotate on use, so the cache holds
  the only live copy after the first refresh.
- Prove it once before trusting a deadline to it: `fpl-buddy token --refresh`.

```bash
.venv/bin/fpl-buddy token       # expiry, and whether it can renew itself
```

## Notifications

`NOTIFY_CHANNEL=discord` posts each proposal as an embed with Approve / Amend /
Reject buttons in a channel of your choice -- a phone notification you can act
on without opening the review link, though that link still works too. The
buttons call the same `Orchestrator` methods as the web page and the CLI; see
[deployment.md#discord](docs/deployment.md#discord) for bot setup.
`log` / `smtp` / `webhook` remain the other options.

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

Docs: [deployment](docs/deployment.md) ·
[day-to-day operations](docs/operations.md) ·
[verifying the write payloads](docs/verify-payloads.md) ·
[decision log](docs/decisions.md)

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
