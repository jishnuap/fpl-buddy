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

**Discord buttons are dynamic, not view instances kept in memory.** A
``discord.ui.View`` posted with a message normally stops working the moment
the process that created it restarts -- the gateway library has no idea what
that button was supposed to do anymore. ``discord.ui.DynamicItem`` sidesteps
this by matching a regex against the button's ``custom_id`` (``fpl:approve:
<proposal id>``) and reconstructing the item from that alone, so a button
posted before a deploy still works after it. This is also why each action gets
its own registered pattern rather than one shared one: a single generic regex
would make every dynamic item class claim to match every other action's
buttons.

**Discord button callbacks run the orchestrator in a thread, never inline.**
`Orchestrator.approve`/`amend` block on HTTP and, for amend, an LLM call --
running that directly in a component callback would freeze the bot's entire
event loop (every other interaction, heartbeats included) for as long as it
takes. `asyncio.to_thread` is the fix; the component interaction is deferred
first so Discord doesn't time out the 3-second ack while that runs.

**Amending posts a new message; it does not edit the old one in place.**
`Orchestrator.amend()` produces a genuinely new `Proposal` (new id, revision +
1) and marks the old one `superseded` -- the Discord surface mirrors that
rather than hiding it: the old message is edited to show it's superseded and
loses its buttons, and the revision is a fresh message with its own. Editing
the old message's content to *look like* the new proposal would hide that a
new id now exists, which matters if you ever need to refer to it (`fpl-buddy
show <id>`, the stored audit trail).

**The bot lives inside the one existing process, not a second one.** The
scheduler already requires exactly one always-on replica
(see "One always-on replica" above); adding a separate bot process would be a
second thing that could drift out of sync with it or double up, for no
benefit -- the bot is started as a background asyncio task on the same event
loop the API runs on.

**Notes are captured passively and folded in once, not chatted with.** The
explicit product decision here is no back-and-forth conversation with the
agent -- `on_message` files every message in the configured channel away as a
`Note` and never replies. The only place a note is ever read is the next
`Orchestrator.propose()` run, which folds everything pending into the brief as
one more input (alongside the squad, fixtures, and projections) and then marks
those notes consumed. This keeps the mental model simple: one proposal per
gameweek, built from one brief, and a note dropped mid-week either makes it
into that brief or it doesn't -- it never lingers to be replayed into a later
gameweek by accident. `amend()` deliberately does not also pull in pending
notes: the human note passed to `amend` is already explicit, immediate
feedback, and mixing in whatever else was typed earlier the same day would
make one amend's reasoning depend on unrelated, differently-timed input.

**The FPL API is the xG source; there is no Understat scraper.** The obvious
next move for better data looks like scraping Understat or FBref. It isn't
needed: `bootstrap-static` ships 105 fields per player and already carries the
Opta xG family (`expected_goals_per_90`, `expected_assists_per_90`,
`expected_goal_involvements_per_90`, `expected_goals_conceded_per_90`), set-piece
taker order, `starts_per_90`, and FPL's own `ep_next` projection. The payload was
already being downloaded in full on every run while `models.py` declared 14 of
those fields, so the highest-value data work was reading what had already
arrived rather than adding a fragile HTML scraper for a worse version of it.
Declaring a field costs one line and no requests; scraping costs a dependency, a
ToS question, and a parser that breaks on redesign. Understat still has
shot-level detail the FPL API lacks -- that is the only reason to revisit this.

**The fixture horizon is a separate request, and optional.** `/fixtures/` with
no arguments returns finished fixtures too, so the horizon uses `?future=1` and
is filtered to `FIXTURE_HORIZON_GAMEWEEKS`. It is fetched in its own try/except
and degrades to this gameweek alone, because a transfer judged on one fixture is
worse than one judged on five, but no proposal at all is worse than both. Before
this existed, `club_fixtures` answered "this gameweek only" while the scout
prompt demanded reasoning "over the next two or three gameweeks" -- the prompt
was asking for something the data could not support.

**Unlimited free transfers are rendered as "unlimited", never as "15".**
`transfers.limit` is `null` pre-season and on a wildcard, which the client maps
to the sentinel `UNLIMITED_FREE_TRANSFERS = 15`. Putting that number straight
into the brief was a real bug with real symptoms: the agent read "Free
transfers: 15" as a large but finite budget, applied the standing "rolling is
frequently correct" guidance, and proposed no transfers at all during pre-season
when every transfer was free. The brief now says "unlimited" and adds an
explicit section inverting the default, and the prompt branches on which
situation you are in rather than always preferring the roll.

**The brief hands the agent a pre-filtered captain shortlist.** Telling a model
"the captain must be in your squad" is not sufficient, and the failure mode was
consistent: the Solio leaderboards in the brief are league-wide and carry `id=`
values, so the model captained the best player in the *league* -- Haaland, whom
it did not own -- and every proposal died on `captain_not_in_squad`. Prompt
wording alone did not fix it; a `## Legal captain / vice options` section
computed from the squad did. Where a constraint can be enforced by only ever
showing legal choices, do that instead of asking politely.

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
