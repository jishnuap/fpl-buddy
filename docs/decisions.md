# Decision log

Decisions that are not obvious from the code, and the reasoning that would
otherwise be lost.

## Settled before the first line was written

**Autonomy: propose → human window → auto-commit at the deadline.** Full
autonomy means a bad week you never saw coming. Full manual means a missed
deadline when you're busy. Silence-as-consent, resolved *at the last moment*,
gets the best of both: you keep a veto, and the default outcome is a submitted
team rather than an empty one.

**The LLM never calls a write endpoint.** It emits a structured `AgentProposal`;
deterministic code validates it and POSTs. This is the difference between a
hallucinated element id being a caught error and being the wrong player in your
squad.

**`deepagents` over plain LangGraph.** Subagents, a filesystem backend and
structured output come for free, and the graph is still a LangGraph graph if it
ever needs surgery.

**One always-on replica.** The scheduler is in-process, so a single instance is a
correctness constraint, not a cost decision.

**A local `.venv`.** No uv, no global pip.

## Decided while building the rest

**Approval executes immediately, by default.** A human tapping "Approve" on a
phone means "do it". `EXECUTE_ON_APPROVAL=false` is there for the more cautious
ordering — record intent, submit at T-45m against fresher data — and it's the
better setting once the payloads are trusted.

**Following an approval link never changes anything.** Mail clients, chat
previews and link scanners fetch URLs unprompted. A `GET` that approved would
mean a spam filter could pick your captain. `GET /a/{token}` renders a review
page; the buttons `POST` back.

**The signed token is the only credential.** No accounts, no sessions. Scoped to
one proposal id, expiring (`APPROVAL_LINK_TTL_HOURS`), and revocable en masse by
rotating `APPROVAL_SECRET`. Read endpoints get a separate optional `API_KEY`,
because leaking your squad matters less than leaking the ability to submit.

**Re-anchor the schedule daily instead of assuming a weekly cadence.** Deadlines
move for international breaks, cup weeks and rescheduled fixtures. The jobs are
derived from the live `bootstrap-static` deadline every morning.

**A late start proposes anyway.** If the process boots inside the propose window
(a deploy at T-6h), it proposes two minutes later rather than skipping the
gameweek — unless a proposal for that gameweek already exists.

**Notification failures are swallowed.** A dead SMTP server must not stop a
proposal from being stored and auto-committed. They're logged at ERROR.

**Unknown Solio club code ⇒ unmatched, not a global name search.** Solio
identifies players by name plus a three-letter club code. If that code doesn't
match an FPL `short_name`, falling back to a league-wide fuzzy name match will
happily return the same surname at the wrong club — precisely the failure that
transfers in the wrong player. Unmatched rows are reported to the agent as
unusable instead.

**`points_hit` is corrected, not rejected.** If the model miscounts the hit, the
validator overwrites it from the real free-transfer count and records a
non-fatal issue. The *ceiling* (`MAX_POINTS_HIT`) is what's fatal. A model that's
bad at arithmetic isn't a reason to throw away good football reasoning.

**Prices always come from the API, never from the model.** `selling_price` is
read from your own `my-team` pick (it differs from `now_cost` under the
50%-of-rise sell-on rule) and `purchase_price` from the target's current
`now_cost`. Both are overwritten in the proposal before anything is submitted.

**Fixture deadlines in tests are rewritten relative to `now`.** Otherwise the
suite starts failing the day the recorded deadline passes, and a test suite that
rots is a test suite people stop trusting.

**Ship an image, not infrastructure-as-code.** The repo had a Container Apps
bicep template and a deploy script; both are gone. Deployment is manual, the
artifact is a Docker Hub image, and [deployment.md](deployment.md) states the
environment contract and the constraints any host has to satisfy. One less thing
to keep in sync with a cloud provider's API version.

**Publishing is triggered by a tag, not by a merge.** CI builds the image on
every pull request to prove the Dockerfile works, and pushes nothing. `v*` tags
and manual dispatch publish. An accidental merge cannot ship an image.

**FPL's OAuth migration made token refresh mandatory.** The handoff described
auth as `pl_profile` + `sessionid` cookies. FPL now issues a PingOne
`access_token` (8h) and `refresh_token` (~180 days) instead, and
`/api/my-team/{entry}/` returns `403 "Authentication credentials were not
provided."` unless the access token is sent as a bearer header. Since a gameweek
cycle spans 36 hours and the access token lasts 8, the token held when a proposal
is made is *always* dead by the time it would be submitted — so refresh is part
of the core loop, not a nicety. The client asks the authenticator for credentials
on every request rather than caching them for the life of the process, for the
same reason.

Confirmed empirically rather than assumed: the OIDC discovery document advertises
the `refresh_token` grant, and a probe with a deliberately invalid token returns
`invalid_grant` (not `invalid_client`) when `client_id` is sent with no secret —
so FPL's OAuth client is public and refresh needs no secret we don't have. The
issuer and client id are read from the token's own claims, so an FPL-side move to
a different PingOne environment needs no code change.

**`verify_session()` probes `/my-team/`, not `/me/`.** After the OAuth migration
`/me/` returns `200` for a cookie jar with no usable access token, so a check
against it reported a healthy session that could not read the squad or submit
anything. A pre-flight check that passes when the thing it protects is broken is
worse than no check.

## Deliberately not done

- **Multi-entry support.** One team, one entry id.
- **Price-change prediction.** Separate problem, different data.
- **Automatic retry of a `failed` execution.** A partial submission needs eyes,
  not a loop.
- **Mini-league scraping / rank-aware strategy.** Interesting, and a whole
  project of its own.

## The one change that would make idle hosting free

The in-process scheduler is what forces an always-on instance, and that instance
is ~98% of the hosting cost — it does about twenty minutes of real work a month.

Moving to a platform scheduler would remove it: authenticated `/jobs/reanchor`,
`/jobs/propose` and `/jobs/commit` endpoints, a daily cron hitting `reanchor`, and
`reanchor` reading the live deadline and enqueueing the two precise runs with a
delayed-task service (Cloud Tasks, or equivalent). Plain cron is not enough on its
own: FPL deadlines move for international breaks and rescheduled fixtures, which
is exactly why the scheduler ended up in-process to begin with.

Two consequences to plan for if you do it:

- `STATE_BACKEND=file` stops being viable — nothing on local disk survives, so the
  store has to be a real database.
- The on-disk cookie cache in `fpl/auth.py` becomes useless, so `FPL_COOKIE_HEADER`
  becomes mandatory rather than a fallback. Login-based auth would re-login on
  every cold start and get rate-limited.
