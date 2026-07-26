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
correctness constraint, not a cost decision. (Still true of *that* deployment.
`SCHEDULER_ENABLED=false` plus `fpl-buddy tick` is now the alternative — see
"Cron ticks instead of a resident scheduler" below.)

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

**A proposal that fails the guardrails is handed back, not just rejected.** The
first live run of the season came back captaining Haaland — who wasn't in the
squad — and starting a player it had transferred out in the same proposal. Both
were caught, so nothing unsafe happened, but the human got an unsubmittable
proposal and no move.

The prompt already forbade both in bold, and the brief already carried a
squad-only captain shortlist. Adding a third restatement was not going to work.
Two things changed instead. The Solio boards now tag every row `[OWNED]` or
`[not owned]` and call the ownership column `sel %`, because "own 74%" sitting
next to an element id reads as *you own him*. And `run_agent` re-validates its
own output: on a fatal issue it continues the same conversation with the exact
errors plus the fifteen ids the squad will contain **after the agent's own
transfers**, computed by `resolved_squad_ids`. The failure was arithmetic, so
the repair hands over the arithmetic rather than the rule.

`AGENT_REPAIR_ATTEMPTS` (default 2) bounds the retries. When they run out the
invalid proposal is still returned and stored with its issues attached: raising
would leave the human with nothing to look at, and a flagged bad proposal is
more useful than silence.

**The captain shortlist admits what it can't know.** It's built before the agent
decides anything, so it lists players that may be sold and omits players that
may be bought. It now says so. Presenting a pre-transfer list as the definitive
set of legal captains is a claim the agent can catch out — and an agent that
catches the brief lying starts discounting the rest of it.

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

`infra/{azure,gcp}/deploy.sh` came back later, and the distinction still holds:
they are `az` and `gcloud` commands in the order you would type them, not a
template DSL with a provider version to track. The scale-to-zero shape is a
dozen interdependent resources — a file share linked to an environment, a
service account bound to a bucket, a scheduler bound to a job — and writing that
out by hand from prose was not reasonable. They do not track drift or tear
anything down, which is the price of not being IaC.

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

**Harvested articles are indexed in the brief, never inlined.** The archive
grows every day; the brief must not. So the brief carries one line per recent
article -- id, date, headline, tags -- and the agent pulls detail with
`read_article` / `search_articles` / `articles_about`. Inlining summaries would
make the per-run token cost a function of how long harvesting has been running,
which is the wrong thing to make it a function of.

**The index is a window; the tools are not.** These are separate on purpose, and
the first implementation got it wrong: all three tools filtered the same recent
list the brief renders, so with a 15-article index and 24 on disk, nine were
unreachable by any means and the documentation claiming otherwise was simply
false. The brief still shows only `KNOWLEDGE_INDEX_DAYS`/`KNOWLEDGE_INDEX_LIMIT`
worth, which is what keeps its cost fixed, but the tools query the store
directly. The case that decides it: a set-piece or injury note written three
weeks ago still bears on captaining someone today, and it leaves the index long
before it stops mattering. `ttl_days` is the real expiry, and pruned notes stay
unreachable through either path.

The archive is read per tool call rather than cached. Notes are a few KB each and
an agent makes a handful of these calls, so a cache would buy microseconds while
being wrong about anything harvested since the process started.

**Reading the archive does not depend on the harvest being configured.**
`KNOWLEDGE_SOURCES_FILE` says whether the daily job should *collect* articles,
which is a different question from whether there are any to read. Gating both on
it meant a `propose` run without that variable set silently reasoned with no
articles at all -- no index, no tools, no error. `open_archive()` keys on notes
existing instead, so a moved or unset source list cannot quietly cost the agent
everything already collected.

**Feeds first, crawling last.** A feed is the publisher stating what is new, in
a format that does not change on redesign; one request answers "anything new?"
for a whole site. Crawling listing pages is the fallback, and if the feeds
already produced a full run's worth of the newest articles the crawl is skipped
entirely rather than fetching pages whose results would be trimmed anyway.

`max_depth` defaults to **0** -- the configured roots only -- after the first
version defaulted to 1 and turned four listing pages into hundreds of requests
by following every nav, tag and pagination link it found. `max_pages_per_run` is
the backstop for when a site's link structure defeats the URL patterns anyway.
Being a good guest matters more here than completeness: this runs against
someone else's site every single day, unattended.

**No paywall circumvention.** Fantasy Football Scout is freemium: an article
returns `200` with roughly its first fifth and a signup pitch, and the rest is
never sent to a logged-out client. No crawler and no headless browser recovers
it -- rendering JavaScript cannot materialise text the server withheld. The
only supported way to get the whole thing is `cookie_env`, naming an environment
variable holding your own subscription cookie, which is authenticated access
rather than a bypass. Deliberately absent: crawler-UA spoofing, cache and AMP
endpoints, archive mirrors. Those defeat an access control on someone's
commercial content, and they break silently besides.

Extraction detects the cut and marks the note `access: partial`, because
summarising an intro as though it were the analysis is worse than knowing you
only have the intro.

**Article fetching is a chain, and only fetching differs between links.** The
first version had Firecrawl return markdown and skip the extractor, on the
grounds that it had already done the work. Measured against a real site, that
was wrong: on Fantasy Football Scout, `only_main_content` markdown came back at
**36,000 characters** of comment threads and navigation wrapped around a **1,900
character** article, while `trafilatura` on the same page returned exactly the
article. Feeding the summariser the former costs most of its input budget and
produces a worse note.

So Firecrawl is asked for markdown *and* HTML, and the HTML goes through the
same extractor every other backend uses. All three now produce byte-identical
text on the same page. Firecrawl's value is reduced to what it is actually
better at -- reaching pages that plain HTTP cannot, by rendering JavaScript and
getting past bot protection -- and boilerplate removal stays in one place where
it can be reasoned about. Markdown is kept only as the fallback for a page that
yields no HTML.

**Firecrawl is metered, so it is spent only where it counts.** One credit per
page, 1000 a month on the free tier. A daily harvest of 26 articles is 780 a
month, which leaves little room for error, so feeds, sitemaps, `robots.txt` and
listing pages are always fetched with plain HTTP -- they are cheap XML and HTML
that needs no rendering, and routing them through Firecrawl would add roughly
another 150 a month for nothing. Remaining credits are read from
`get_credit_usage()` rather than tallied locally, because a local counter misses
usage from elsewhere and resets exactly when state is lost.
`FIRECRAWL_CREDIT_RESERVE` stops the harvester before it spends the last of the
month's budget.

**Rendering surfaces junk that plain HTTP never sees.** A headless browser
collects consent walls, "enable JavaScript" notices and extension-block
interstitials, and they arrive *ahead* of the article -- the BBC's pages came
back beginning with `ERR_BLOCKED_BY_CLIENT`. Those leading lines are trimmed
before summarising. Only the opening of a document is examined, so an article
that merely discusses cookies halfway down is left alone.

**A YouTube channel is a source kind, not a second pipeline.** Discovery uses
the per-channel upload feed and the content is the caption track; everything
after that -- summarising into the same schema, resolving player names, the
markdown note, the TTL -- is shared with articles. Only two steps differ: there
is no extraction to do, because captions are already text with no boilerplate,
and the input budget is separate. A half-hour video runs to ~28,000 characters
against an article budget of 12,000, so reusing it would discard most of the
video and then report it as truncated. `TRANSCRIPT_INPUT_CHARS` handles it in
one call; chunk-and-merge was the alternative and costs more tokens *and* more
failure modes for a problem a larger budget solves outright.

Speech recognition mangles surnames, which would matter if names were matched
against the raw captions. They are not: ids resolve from the *summary's*
`player_names`, which the model spells correctly from context, so the existing
design absorbs most of it.

Timestamps are marked through the transcript roughly once a minute, so a claim
can cite a point in the video and be checked against it -- the same provenance
role the stored `Source extract` plays for articles.

**A channel id comes from the page's canonical link, never from the first one
that looks right.** The first `"channelId"` in a YouTube channel page belongs to
something else -- a recommendation, or the owner of an embedded video. Matching
it resolved `@LetsTalkFPL` to "Let's Talk Football", a different channel, and
nothing downstream noticed: the feed parsed, transcripts fetched, notes stored,
and the archive quietly filled with international-tournament reaction instead of
FPL. The `<link rel="canonical">` id is the page stating its own address, with
`externalId` as a second opinion. There is deliberately no fallback to a loose
match, because a wrong-but-plausible id is exactly the failure that hides.

**Robots exceptions are per source, written down, and off by default.** YouTube
disallows both `/feeds/videos.xml` and `/api/` to crawlers, and the transcript
library brings its own HTTP client that never passes through `fetch.py` at all.
Left implicit, that would quietly falsify the "we honour robots.txt" property
the rest of the harvester has. `ignore_robots: true` makes the exception a thing
you can grep for, and a YouTube source that omits it is warned at load time that
it will find nothing. The question of whether to set it is the operator's, and
YouTube's terms on automated access are a separate one from robots.

**Untrusted web text is contained at the summariser, not at the reader.** This
is the first feature that puts arbitrary web prose into a prompt that drives real
team decisions, so the boundary is drawn where the text arrives:

- the summariser is asked for a **fixed pydantic schema**, so a page cannot emit
  anything but `summary`/`key_points`/`player_names`/`tags`. It can still argue
  for a bad transfer -- which is all any bad tipster can do -- but it cannot
  issue instructions;
- **element ids never come from the model.** Names are resolved afterwards
  against `bootstrap-static`, so an article cannot introduce an id at all;
- the article text is fenced in the prompt as untrusted, and every rendering
  (brief section, tool output, stored extract) says so in words;
- the existing invariants still hold underneath: no write tools, deterministic
  validation, human approval.

**Player names resolve by token match, not fuzzy score.** The first version used
`rapidfuzz.WRatio` and attached an article about Coventry's *Raphael Borges
Rodrigues* to Brentford's *Igor Thiago Nascimento Rodrigues*, because WRatio
rewards a shared token inside a long name. Now a name resolves only when it is
unambiguous: a unique exact match, or every mentioned token belonging to exactly
one player, or a surname only one player answers to. Bare "Palmer" resolves to
nothing while "Cole Palmer" resolves correctly. Attaching news about a player
you do not own to one you do is worse than attaching nothing, so ambiguity is
dropped rather than guessed -- the same reasoning as the projections joiner.

## Deliberately not done

- **Multi-entry support.** One team, one entry id.
- **Price-change prediction.** Separate problem, different data.
- **Automatic retry of a `failed` execution.** A partial submission needs eyes,
  not a loop.
- **Mini-league scraping / rank-aware strategy.** Interesting, and a whole
  project of its own.

## Cron ticks instead of a resident scheduler

The in-process scheduler was what forced an always-on instance, and that instance
was ~98% of the hosting cost — it does about twenty minutes of real work a month.
`fpl-buddy tick` is the alternative: a platform cron runs it every ten minutes,
and each run asks what is due and does it. See [serverless.md](serverless.md).

**Polling, not delayed tasks.** An earlier sketch of this had `reanchor` reading
the live deadline and enqueueing two precise runs with Cloud Tasks. Polling won
on failure mode: a dropped enqueue means nothing commits and nothing says so,
whereas a missed poll costs ten minutes. It also needs no queue service to
exist. Precision was never worth much here — the commit window is 45 minutes
wide precisely so it does not need to be hit exactly.

**Both drivers share `schedule.plan_for()`.** Two implementations of "when does
the propose window open" would eventually disagree, and the symptom would be one
deployment behaving differently from the other for reasons no log line explains.

**Idle ticks must be nearly free, or the cost argument collapses.** Nothing
above the "nothing due" exit imports the agent stack or touches the network. The
ledger caches the next deadline so most ticks are one small file read; the live
deadline is re-read every few hours while it is distant, and every tick once the
window is close, which is when a moved deadline actually matters.

**One switch governs the scheduler and the Discord gateway.** They are different
features with one thing in common: each keeps a container alive. Separate
switches would let you turn off half of it and get a service that still cannot
idle, having changed nothing about the bill.

**The earlier note here was wrong about state, in both directions.** It said
`STATE_BACKEND=file` stops being viable and the store has to become a real
database. In fact both platforms mount a network filesystem onto scale-to-zero
compute (Azure Files, GCS FUSE), so `file` works unchanged — and `azure_table`
would not have saved you anyway, because `fpl_cookies.json` sits on `state_dir`
regardless of the backend. A durable filesystem is required either way, which
makes it the simpler choice rather than the compromise.

**`EXECUTE_ON_APPROVAL=false` in that deployment, and not as caution.** With two
processes sharing state, it is the thing that keeps every FPL write inside one
of them. The refresh token rotates on use, so two concurrent refreshes leave the
loser holding a dead token; approving through the web service would be exactly
that if it submitted directly.

**Discord buttons are dropped rather than posted broken.** Without a gateway
there is nothing listening for the interaction, and a button that silently fails
when tapped is worse than a link that works. Getting them back means an
interactions endpoint URL and Ed25519 signature verification, which is a
feature, not a config change.
