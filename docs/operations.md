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
fpl-buddy verify              # is the session alive?
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

**`403` on login.** Expected from a datacenter IP. Use `FPL_COOKIE_HEADER`; see
[deployment.md](deployment.md#the-cookie-header).

**Session dies mid-week.** The client refreshes once on a `401`/`403` and retries.
If that fails, `verify` says so and the propose job logs it. Re-paste the cookie.

**Proposal failed validation.** The notification lists every reason. It will not
be submitted — the guardrails run again at commit time and block it there too.
Amend it, or just do the move yourself in the app; a superseded proposal is
harmless.

**`failed` after transfers went through but picks didn't.** The executor records
this honestly (`Transfers applied but picks failed`). Your squad changed but the
armband didn't — set the captain in the app. Do not re-run `commit`: it would try
the transfers again.

**Nothing happened at the deadline.** Check, in order: was the container running
(scale-to-zero or a crash loop kills the scheduler); did a proposal exist for that
gameweek (`fpl-buddy list`); was it `pending` (a `rejected` one is left alone);
is `AUTO_COMMIT_ENABLED=true`.

**Solio returns 403.** Some networks and proxies block it. Projections are
optional — the run continues and the brief says they were unavailable.

## Safety switches

| Setting | Effect |
|---|---|
| `DRY_RUN=true` | No POST leaves the process. The default. |
| `MAX_POINTS_HIT=0` | Any plan costing points is blocked as a fatal issue. |
| `AUTO_COMMIT_ENABLED=false` | Untouched proposals expire instead of submitting. |
| `EXECUTE_ON_APPROVAL=false` | Approval records intent; the commit job submits later against fresher data. |
| `MIN_CAPTAIN_CONFIDENCE=0.5` | Blocks execution when the agent isn't confident. |
| Rotate `APPROVAL_SECRET` | Every outstanding approval link dies immediately. |

## What the agent can and cannot do

It reads. That is the whole of it: `bootstrap-static`, fixtures, player
summaries, the Solio boards, and your squad. There is no tool in
`agent/tools.py` that writes, and the executor is the only code that POSTs.
A hallucinated element id fails `validate()` and never reaches the network.

If you add a tool, keep it a getter. That invariant is the reason this is safe to
leave running.
