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

That container holds the scheduler, so it has to stay up.
[docs/deployment.md](docs/deployment.md) has the environment contract and the few
rules any host has to satisfy (one instance, never scaled to zero, durable
`STATE_DIR`).

If you would rather not pay a cloud for an instance that idles between
gameweeks, [docs/serverless.md](docs/serverless.md) is the same image driven by a
platform cron instead — a `fpl-buddy tick` job plus a web service that scales to
zero, at roughly £0.50/month against ~£24. One script per cloud:

```bash
IMAGE=youruser/fpl-buddy:0.1.0 ./infra/azure/deploy.sh
PROJECT=my-project IMAGE=youruser/fpl-buddy:0.1.0 ./infra/gcp/deploy.sh
```

The tradeoff is Discord's buttons and passive note capture, both of which need a
gateway connection that would keep the container alive anyway.

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

## What the agent sees

The brief is built fresh each run from:

| Source | Carries |
|---|---|
| `bootstrap-static` | Prices, availability, **Opta xG/xA/xGI/xGC per 90**, `starts_per_90` (rotation risk), **set-piece taker order**, FPL's own `ep_next`, defensive contribution, transfer momentum |
| `my-team` | Your 15 with selling prices, bank, free transfers, chips |
| `fixtures?future=1` | The fixture run over the next `FIXTURE_HORIZON_GAMEWEEKS` (default 5), not just the gameweek being submitted |
| `team/set-piece-notes` | FPL's official set-piece notes, when published |
| Solio Analytics | Projection leaderboards — treated as signal, never as the source of truth for ids or prices |

There is no Understat or FBref scraper here on purpose: the FPL API already
serves the Opta xG family, so the underlying numbers come from an endpoint that
is already authorised and already being downloaded. See
[decisions.md](docs/decisions.md).

Two things the brief does deliberately, because getting them wrong cost real
proposals: it renders unlimited free transfers as **"unlimited"** rather than the
sentinel `15` (an agent told it has "15 free transfers" reads a finite budget and
rolls), and it hands over a pre-computed **legal captain shortlist** drawn only
from your squad — the projection leaderboards are league-wide, and prompt wording
alone did not stop the agent captaining a player it did not own.

## Harvested articles

A daily job collects FPL tips and team news from sources you configure, writes
one markdown file per article, and lets the agent read them while it reasons.

```bash
cp sources.example.yaml sources.yaml     # edit; then set KNOWLEDGE_SOURCES_FILE
.venv/bin/fpl-buddy harvest --dry-run    # what would be collected
.venv/bin/fpl-buddy harvest              # collect and summarise
.venv/bin/fpl-buddy articles             # what's in the store
```

Sources are entirely config-driven: feeds, sitemaps, or listing pages used as
crawl roots, with URL patterns, caps and a per-source TTL. A source can also be
a **YouTube channel** (`kind: youtube`) -- discovery reads the channel's upload
feed and the content is the video's caption track, summarised into the same
notes as everything else. That needs `ignore_robots: true` per source, because
YouTube disallows both the feed and the caption endpoint to crawlers; it is off
everywhere by default so the exception stays visible. Notes land in
`${STATE_DIR}/knowledge` as markdown with a YAML header whose fields follow
schema.org `Article`, so the archive is readable in Obsidian or any static site
generator and outlives this project.

The brief gets a one-line **index** of recent articles only
(`KNOWLEDGE_INDEX_DAYS`, `KNOWLEDGE_INDEX_LIMIT`), so its token cost is fixed no
matter how large the archive gets. The tools read the **whole archive**, past
that window: `read_article(id)`, `search_articles(query)` and
`articles_about(element_id)`. A set-piece change written three weeks ago still
decides a captaincy call today, and it drops off the index long before it stops
mattering. Notes past their source's `ttl_days` are pruned and unreachable
either way.

Reading the archive does not depend on `KNOWLEDGE_SOURCES_FILE`: that setting
says whether the daily job should *collect* articles, which is a different
question from whether there are any to read.

> **Harvested text is untrusted.** It is fenced as data at the summariser, which
> can only emit a fixed schema; element ids are resolved from
> `bootstrap-static`, never taken from an article; and every rendering says it is
> third-party opinion. A hostile page can argue for a bad transfer — as any bad
> tipster can — but not issue instructions, and deterministic validation plus
> your approval still sit underneath. See [decisions.md](docs/decisions.md).

Article pages are fetched by the first available backend in
`KNOWLEDGE_FETCH_BACKENDS`: **Firecrawl** (renders JavaScript, gets past bot
protection; 1 credit/page, optional), **Scrapling** (local, free, browser TLS
impersonation; optional), then **httpx** (always there). Only article pages use
them — feeds and `robots.txt` stay on plain HTTP, which keeps a 26-article daily
harvest to ~780 of Firecrawl's 1000 free monthly credits.

Whichever backend fetches, the same extractor runs on the HTML, so all three
produce identical text. That is deliberate: Firecrawl's own markdown returned
36k characters of comment threads around a 1.9k article on one site.

Paywalled sources yield only their free portion, marked `access: partial`. If you
hold a subscription, `cookie_env` names an environment variable holding your own
session cookie. There is no paywall circumvention here and there won't be —
verified: Firecrawl does not recover gated sections either, because the server
never sends them.

## Notifications

`NOTIFY_CHANNEL=discord` posts each proposal as an embed with Approve / Amend /
Reject buttons in a channel of your choice -- a phone notification you can act
on without opening the review link, though that link still works too. The
buttons call the same `Orchestrator` methods as the web page and the CLI; see
[deployment.md#discord](docs/deployment.md#discord) for bot setup.
`log` / `smtp` / `webhook` remain the other options.

The same channel doubles as a place to leave notes during the day -- no chat,
just a message ("captain Oakley this week") that gets folded into the next
scheduled proposal and then forgotten. A 📝 reaction confirms it was seen.

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
  data/              Solio projections + the DecisionContext brief (squad,
                     underlying numbers, fixture horizon, captain shortlist)
  decisions/         schema, guardrails, store, executor
  agent/             Azure OpenAI + deepagents, prompts, read-only tools
  orchestrator.py    propose / approve / reject / amend / auto-commit
  api.py             FastAPI + signed one-click approval links
  schedule.py        when to propose and commit, derived from the live deadline
  scheduler.py       APScheduler, for the always-on deployment
  tick.py            the same schedule as a cron job, for the scale-to-zero one
  ledger.py          what the tick driver remembers between invocations
  notify.py          log / smtp / webhook / discord channel selection + rendering
  discord_bot/       gateway bot: embeds, Approve/Amend/Reject buttons, the async
                     bridge -- plus a gateway-free notifier for the cron job
  notes.py           notes captured from Discord, folded into the next proposal
  knowledge/         daily article harvest: sources, crawl, extract, summarise,
                     markdown store the agent reads on demand
  cli.py             typer entrypoints
```

Docs: [deployment](docs/deployment.md) ·
[running it without an always-on container](docs/serverless.md) ·
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
