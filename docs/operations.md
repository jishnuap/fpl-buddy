# Running it

## The weekly rhythm

| When | What happens |
|---|---|
| T-36h | Propose job: build the brief, run the agent, validate, store, notify with a signed link. |
| T-36h → T-45m | Your window. Approve, reject, amend, or ignore. |
| T-45m | Commit job: **rebuild the context from scratch**, re-validate, submit if still `pending` or `approved`. |
| T-2m | Hard refusal window. Nothing is submitted this close to the deadline. |

## Proposal states

```
pending ──approve──> approved ──> executed
   │                      │
   │                      └──(revalidation fails)──> failed
   ├──reject───> rejected
   ├──amend────> amended ──> superseded   (a new revision takes over)
   ├──silence──> auto_executed            (AUTO_COMMIT_ENABLED=true)
   └──silence──> expired                  (AUTO_COMMIT_ENABLED=false)
```

`failed` is not retried automatically. It means either FPL rejected the request
or re-validation blocked it — both want a human.

## Day-to-day commands

```bash
fpl-buddy verify              # can it actually read the squad?
fpl-buddy token               # token expiry, and whether it can renew itself
fpl-buddy token --refresh     # force a refresh, to prove the flow works
fpl-buddy schedule            # when will the jobs run?
fpl-buddy context             # exactly what the agent will read
fpl-buddy propose             # run the agent now
fpl-buddy list                # every stored proposal
fpl-buddy show --transcript   # the latest one, with the agent's reasoning
fpl-buddy check               # re-validate against live data, submit nothing
fpl-buddy approve             # submit (respects DRY_RUN)
fpl-buddy amend "captain Oakley, Vasquez is a rotation risk"
fpl-buddy commit              # run the deadline job by hand
```

## When something goes wrong

**`403` on login.** Expected from a datacenter IP, and unavoidable now that FPL
uses OAuth. Use `FPL_COOKIE_HEADER`; see [deployment.md](deployment.md#authentication).

**`403 "Authentication credentials were not provided."` on `/my-team/`.** The
access token is missing or dead. `fpl-buddy token` shows the state; a valid
refresh token renews it automatically, and if refresh is rejected the error says
to re-paste. The header must contain `access_token` **and** `refresh_token` —
without the latter, the session cannot survive its 8-hour lifetime.

**It worked, then stopped after one refresh.** `STATE_DIR` isn't durable. Refresh
tokens rotate on use, so the copy in your environment is spent after the first
refresh and only the cache holds the live one. Mount a volume.

**`verify` passes but the propose job can't read the squad.** Shouldn't happen
now — `verify` probes `/my-team/`. If you see it, check that `FPL_ENTRY_ID` is
set, because with no entry id `verify` falls back to `/me/`, which passes on
almost anything.

**Proposal failed validation.** The notification lists every reason. It will not
be submitted — the guardrails run again at commit time and block it there too.
Amend it, or just do the move yourself in the app; a superseded proposal is
harmless.

**`failed` after transfers went through but picks didn't.** The executor records
this honestly (`Transfers applied but picks failed`). Your squad changed but the
armband didn't — set the captain in the app. Do not re-run `commit`: it would try
the transfers again.

**Nothing happened at the deadline.** Check, in order: which scheduler was
supposed to fire (`curl .../healthz` — `in-process` means the container has to
have been running, and a crash loop or an unintended scale-to-zero kills it;
`external` means the cron job has to have fired, so check its execution
history); did a proposal exist for that gameweek (`fpl-buddy list`); was it
`pending` (a `rejected` one is left alone); is `AUTO_COMMIT_ENABLED=true`.

On the cron deployment, `fpl-buddy tick` prints what it decided and why —
"nothing due (next deadline in 3d 4h)" is a healthy answer, and the executions
list is where a job that never ran shows up as an absence.

**Solio returns 403.** Some networks and proxies block it. Projections are
optional — the run continues and the brief says they were unavailable.

**The proposal never suggests any transfers.** Check what the brief says about
free transfers (`fpl-buddy context | head -5`). If you have unlimited transfers
it must read "Free transfers: unlimited" and the brief must carry a "Transfers
are free this gameweek" section — with a bare number there instead, the agent
reads a finite budget and rolls. In a normal one-or-two-transfer week, rolling is
frequently the correct answer and not a bug; check the proposal's reasoning
before assuming otherwise.

**The harvest collects nothing.** Run `fpl-buddy harvest --dry-run`: it lists
every candidate URL per source and marks each `new` or `known`. No candidates at
all usually means the `include_patterns` don't match the site's article URLs —
check a real article's path. All `known` means it is working and there is simply
nothing new. If a feed 404s or redirects to HTML, drop it and use `roots`
instead; a redirect to a listing page is not a feed.

**Articles are stored but the agent ignores them.** The brief indexes only
articles within `KNOWLEDGE_INDEX_DAYS` (up to `KNOWLEDGE_INDEX_LIMIT`) — but the
tools search the whole archive, so an article missing from the index is still
reachable via `search_articles` or `articles_about`. What is genuinely gone is
anything past its source's `ttl_days`, which the harvest prunes. `fpl-buddy
articles` shows what survives. Beyond that the agent chooses whether to open
one; a proposal that never cites an article is not necessarily a bug.

Reading does *not* require `KNOWLEDGE_SOURCES_FILE` — only harvesting does. If
`fpl-buddy articles` lists notes, the agent can reach them.

**Everything is stored as `access: partial`.** The source is paywalled and you
are fetching it logged out, so only the free portion exists to store. Either
supply a subscription cookie via the source's `cookie_env`, or accept intros
only. No crawler setting changes this — the content is not sent.

**Every proposal fails with `captain_not_in_squad`.** The brief should contain a
`## Legal captain / vice options` section listing only players you own. If it is
missing, the agent is picking from the league-wide projection leaderboards
instead. `fpl-buddy context` shows whether the section is being generated.

**Discord button says "This interaction failed".** Either the container was
mid-restart when you clicked (retry once it's back — buttons are stateless
and survive a restart), or the bot's permissions in that channel are missing
Send Messages / Embed Links. The embed's title is a link to the same review
page the buttons act on, and works regardless.

**Discord never posted anything.** `NOTIFY_CHANNEL=discord` needs both
`DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` — the container refuses to start
without both once that channel is selected. Check the bot is actually in the
server and can see the channel.

**A note you typed in Discord never showed up in the next proposal.** Most
likely `MESSAGE CONTENT INTENT` isn't turned on for the bot (Developer Portal →
Bot tab) — without it the bot can't read message text at all, and would have
failed to connect entirely rather than silently missing messages, so check the
logs for a `PrivilegedIntentsRequired` error first. If it connected fine, check
you typed in the exact channel `DISCORD_CHANNEL_ID` points to; other channels
are ignored. A message with no reaction from the bot was never captured.

## Safety switches

| Setting | Effect |
|---|---|
| `DRY_RUN=true` | No POST leaves the process. The default. |
| `MAX_POINTS_HIT=0` | Any plan costing points is blocked as a fatal issue. |
| `AUTO_COMMIT_ENABLED=false` | Untouched proposals expire instead of submitting. |
| `EXECUTE_ON_APPROVAL=false` | Approval records intent; the commit job submits later against fresher data. |
| `MIN_CAPTAIN_CONFIDENCE=0.5` | Blocks execution when the agent isn't confident. |
| `AGENT_REPAIR_ATTEMPTS=2` | Retries a proposal that fails validation, quoting the errors back. Costs one model call each. `0` disables. |
| Rotate `APPROVAL_SECRET` | Every outstanding approval link dies immediately. |

## What the agent can and cannot do

It reads. That is the whole of it: `bootstrap-static`, fixtures, player
summaries, the Solio boards, and your squad. There is no tool in
`agent/tools.py` that writes, and the executor is the only code that POSTs.
A hallucinated element id fails `validate()` and never reaches the network.

If you add a tool, keep it a getter. That invariant is the reason this is safe to
leave running.
