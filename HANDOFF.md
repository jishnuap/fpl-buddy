# Handoff: `fpl-buddy` — LangChain deep agent for FPL captaincy + transfers

Paste this whole file as the opening prompt of the new coding session.

---

## What I'm building

A Python service that runs on **Azure Container Apps** and manages my Fantasy Premier
League team. Each gameweek it:

1. Builds a factual brief (my squad, budget, fixtures, injury flags, projections).
2. Runs a **LangChain `deepagents` agent** (Azure OpenAI / AI Foundry) that outputs a
   structured proposal: captain, vice, transfers, XI/bench, chip.
3. Validates that proposal with **deterministic, non-LLM guardrails**.
4. Notifies me and **waits**. I can approve, reject, or amend.
5. **If I do nothing, it auto-executes shortly before the deadline.** That "silence =
   consent, but only at the last moment" behaviour is the core product decision.

Repo: `/Users/jishnua/Repos/fpl-buddy` (currently empty).

## Decisions already made — don't relitigate these

| Decision | Choice |
|---|---|
| Autonomy | Propose → human window → auto-commit at deadline if untouched |
| Stack | Python 3.11+, `deepagents` (LangChain), not plain LangGraph |
| LLM | Azure OpenAI via AI Foundry (`AzureChatOpenAI`), key **or** managed identity |
| Hosting | Azure Container Apps |
| Env management | **A local `.venv`** — not uv, not global pip. `python3 -m venv .venv` |
| LLM authority | The agent **never** calls a write endpoint. It emits a structured proposal; deterministic code validates and POSTs. |

## Technical facts I verified — build on these, don't re-research

### FPL authentication

- Login: `POST https://users.premierleague.com/accounts/login/`
  Form fields: `login`, `password`, `app=plfpl-web`,
  `redirect_uri=https://fantasy.premierleague.com/a/login`
- **Do not follow redirects.** Success is a `302` that carries the `Set-Cookie`
  headers. Following the hop loses them and you end up anonymous.
  (`httpx` doesn't follow by default — set `follow_redirects=False` explicitly so
  nobody flips it later.)
- Required cookies: **`pl_profile`** and **`sessionid`**.
- **A `403` from a datacenter IP is expected.** Premier League fronts login with bot
  protection that routinely rejects cloud IPs — which is exactly what a Container App
  is. So the design has a mandatory fallback: `FPL_COOKIE_HEADER`, pasted from
  DevTools → Network → any `/api/me/` request → Request Headers → `cookie`. Treat the
  fallback as the likely production path, not an afterthought.
- CORS blocks all of this from a browser. Server-side only. (Already satisfied.)

### FPL endpoints

Reads (public unless noted):
- `GET /api/bootstrap-static/` — `elements` (players), `teams`, `events` (gameweeks)
- `GET /api/fixtures/?event={gw}` — includes `team_h_difficulty` / `team_a_difficulty`
- `GET /api/element-summary/{id}/` — per-player history + upcoming
- `GET /api/me/` — **auth**, cheap session-validity probe
- `GET /api/my-team/{entry}/` — **auth**. Shape:
  ```
  { "picks":    [{element, position, selling_price, purchase_price,
                  multiplier, is_captain, is_vice_captain}],
    "chips":    [{name, status_for_entry, ...}],
    "transfers":{bank, value, limit, made, cost, status} }
  ```
  `transfers.limit` is free transfers and is **`None` when unlimited** (wildcard /
  pre-season) — handle that or you'll compute a phantom hit.

Writes (auth + `Content-Type: application/json`, `X-Requested-With: XMLHttpRequest`,
`Origin: https://fantasy.premierleague.com`, matching `Referer`, cookie header):

- `POST /api/transfers/`
  ```json
  { "confirmed": true, "entry": <id>, "event": <gw>,
    "chip": null, "freehit": false, "wildcard": false,
    "transfers": [{"element_in": 427, "element_out": 233,
                   "purchase_price": 55, "selling_price": 60}] }
  ```
- `POST /api/my-team/{entry}/` — captaincy, vice, bench order
  ```json
  { "chip": null,
    "picks": [{"element": 1, "position": 1,
               "is_captain": false, "is_vice_captain": false}, ...] }
  ```
  All 15 slots. 1–11 = XI, 12–15 = bench in auto-sub order, **12 must be the reserve
  keeper**.

> ⚠️ **Verify both write payloads against a real browser capture before flipping
> `DRY_RUN=false`.** I assembled them from community sources, not from an official
> spec. Do one transfer manually in the FPL web app with DevTools open, copy the real
> request, and diff it against `submit_transfers` / `submit_picks`.

`selling_price` comes from *your* `my-team` pick and can differ from the player's
`now_cost` because of the 50%-of-rise sell-on rule. `purchase_price` is the incoming
player's current `now_cost`. Never guess either.

### Solio Analytics (projections)

`GET https://fpl.solioanalytics.com/api/data/latest.json` — free, unauthenticated.
Confirmed structure:

- Metadata: `generatedAt`, `gameweek`, `deadlineIso`, `source`
- Leaderboards (each a list of player objects): `topProjected`, `topCaptains`,
  `topDifferentials`, `topGoals`, `topAssists`, `topBonus`, `topDefCon`,
  `bestCleanSheets`, `bestAttackingFixtures`, `topTransfersIn`, `topTransfersOut`
- Player object: `name`, `team` (3-letter code), `position`, `price` (tenths, e.g.
  `155`), `ownership`, `opponents: [{opponent, isHome}]`, `prPoints`, `prGoals`,
  `prAssists`, `prBonusPoints`, `captainProjPoints`, `leverage`

Two gotchas:
- It returns **ranked leaderboards, not a full player table**. It's signal, not a
  dataset. `bootstrap-static` stays the source of truth for ids, prices, availability.
- **No FPL element ids.** You must join name+club → element id. Match club exactly
  first, then fuzzy-match name within that club (`rapidfuzz`, threshold ~82). Report
  unmatched rows to the agent as "do not use as transfer targets" rather than guessing
  — a bad id transfers in the wrong player.

### `deepagents` API (verified signature)

```python
from deepagents import create_deep_agent

create_deep_agent(
    model=...,              # str "provider:model" or a BaseChatModel instance
    tools=...,              # Sequence[BaseTool | Callable | dict]
    *,
    system_prompt=...,      # str | SystemMessage
    middleware=(),
    subagents=None,         # Sequence[SubAgent | CompiledSubAgent | AsyncSubAgent]
    skills=None, memory=None, permissions=None,
    backend=None,           # BackendProtocol; defaults to StateBackend
    interrupt_on=None,      # dict[str, bool | InterruptOnConfig]
    response_format=None,   # <- use this for the structured Proposal
    state_schema=None, context_schema=None,
    checkpointer=None, store=None,
    debug=False, name=None, cache=None,
) -> CompiledStateGraph
```

Pass an `AzureChatOpenAI` **instance** to `model` (the `"provider:model"` string form
won't carry the Azure endpoint/deployment/api-version). Use `response_format` to force
the `AgentProposal` schema. `interrupt_on` is worth a look for the approval gate,
though the design below uses an external store instead so approval survives restarts.

## Architecture

```
scheduler (APScheduler)                    FastAPI
  ├─ T-36h  propose ──┐                      ├─ GET  /health
  └─ T-45m  commit ───┤                      ├─ GET  /proposals/latest
                      │                      ├─ GET  /a/{token}        (signed link)
                      ▼                      ├─ POST /proposals/{id}/approve
        build_context ──> agent ──> validate  ├─ POST /proposals/{id}/reject
              │                        │      └─ POST /proposals/{id}/amend
              │                        ▼
              │                  ProposalStore
              │                        │
              └────────────────> executor ──> FPL POSTs
```

Proposal lifecycle:
`pending → approved | rejected | amended → executed | auto_executed | failed | expired | superseded`

Two jobs:
- **propose** at `PROPOSE_HOURS_BEFORE_DEADLINE` (default 36) — build context, run
  agent, validate, store, notify with signed approve/reject links.
- **commit** at `COMMIT_MINUTES_BEFORE_DEADLINE` (default 45) — if still `pending`,
  **rebuild the context from scratch** (prices/injuries move in 36 hours), re-validate,
  then execute. Any fatal issue ⇒ don't submit, notify instead.

Non-negotiable invariants:
- Re-validate at execution time against a **fresh** context, never the stored one.
- `DRY_RUN=true` by default; no POST leaves the process until it's explicitly false.
- Hard refusal window: don't POST inside ~2 minutes of the deadline (a request that
  lands late leaves you unable to tell whether it applied).
- `MAX_POINTS_HIT` defaults to `0` — never take a hit unless I raise it.

## Code already written

Attached tarball `fpl-buddy-wip.tar.gz`. 14 files, complete and coherent, **untested**
(nothing has been run or imported yet — the container had no deps installed).

```
pyproject.toml                      deps, ruff, pytest config, hatchling
src/fpl_buddy/config.py             pydantic-settings, every knob env-driven
src/fpl_buddy/fpl/models.py         Player/Team/Gameweek/Fixture/Pick/MyTeam/Bootstrap
src/fpl_buddy/fpl/auth.py           login (manual redirect) + cookie-header fallback
                                    + on-disk cookie cache, 403 detection
src/fpl_buddy/fpl/client.py         reads, writes, retry, 401/403 session refresh,
                                    DRY_RUN short-circuit
src/fpl_buddy/data/solio.py         Solio fetch/parse + fuzzy join to element ids
src/fpl_buddy/data/context.py       DecisionContext + render() → the agent's brief
src/fpl_buddy/decisions/schema.py   AgentProposal (LLM contract) + Proposal (stored)
src/fpl_buddy/decisions/validate.py ALL guardrails — squad legality, budget, club
                                    limit, formation, hits, captaincy, deadline
src/fpl_buddy/decisions/store.py    file + Azure Table backends
src/fpl_buddy/decisions/executor.py re-validate → transfers → picks, honest partial
                                    failure recording
+ __init__.py in each package
```

`validate.py` is the file to read first — it's where the safety actually lives.

## What's left

1. **`agent/`** — `build.py` (`AzureChatOpenAI` + `create_deep_agent`, key *and*
   managed-identity auth), `prompts.py`, `tools.py` (read-only: player summary,
   fixture lookup, Solio board, squad inspection — **no write tools**),
   `subagents.py` (e.g. a captaincy specialist and a transfer-target scout).
2. **`orchestrator.py`** — propose / approve / reject / amend / auto-commit flows over
   the store.
3. **`api.py`** — FastAPI, signed approval links via `itsdangerous`
   (`APPROVAL_SECRET`, TTL). One-click approve from a phone matters here.
4. **`scheduler.py`** — APScheduler, jobs anchored to the real deadline from
   `bootstrap-static`, timezone `Asia/Kolkata`.
5. **`notify.py`** — pluggable: `log` / `smtp` / `webhook`.
6. **`cli.py`** — typer: `login`, `context`, `propose`, `show`, `approve`, `commit`,
   `verify`. Needed for testing without waiting for a real deadline.
7. **Packaging** — `Dockerfile`, `.env.example`, `.gitignore` (must exclude `.venv/`,
   `.state/`, `.env`), `Makefile` (`make setup` = venv + editable install),
   `infra/` bicep or `az` script, `README.md`, `docs/`.
8. **Tests** — `pytest` over `validate.py` (every guardrail, both directions),
   payload builders, the Solio join, and `respx`-mocked client calls. Ship a fixture
   `bootstrap-static` and `my-team` so tests need no network.

## Ground rules for the session

- Use a **`.venv`**: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`.
- Don't let the LLM near a write endpoint. Proposal in, validation, then POST.
- Every guardrail gets a test. This code can spend my real points.
- Keep `DRY_RUN=true` until the payloads are diffed against a live browser capture.
- Note: `fpl.solioanalytics.com` was blocked (403) from the sandbox I built this in —
  it should work fine from a normal network, but if it 403s locally that's the proxy,
  not the code.

## Suggested first move

Set up the venv, install, get `ruff check` and a smoke import passing over the
existing 14 files, then write the `validate.py` tests **before** building the agent.
The guardrails are the load-bearing part; the agent is replaceable.
