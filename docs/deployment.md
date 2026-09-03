# Deploying

The deliverable is a container image on Docker Hub plus the environment contract
below; where you run it is your call.

There are two shapes to choose between:

| | This document | [serverless.md](serverless.md) |
|---|---|---|
| Shape | One container, always up | Cron job + a web service that idles at zero |
| Scheduler | In-process | `fpl-buddy tick`, from the platform's cron |
| Cost | ~£20–28/mo on a managed platform, £0 on a machine you already own | ~£0.50/mo |
| Discord | Embed with Approve/Amend/Reject buttons, plus passive note capture | Embed with an approval link; no buttons, no note capture |
| Deploy | `docker run`, below | `./infra/azure/deploy.sh` or `./infra/gcp/deploy.sh` |

Run this one if you have a machine already on. Run the other if you want a cloud
to host it and would rather not pay for idle.

## 1. Publish the image

Tag a release and CI pushes to Docker Hub:

```bash
git tag v0.1.0
git push origin v0.1.0
```

That publishes `<your-dockerhub-user>/fpl-buddy:0.1.0`, `:0.1`, and `:sha-<short>`
for `linux/amd64` and `linux/arm64`. For an unversioned build, run the **Publish
image** workflow manually (default tag `edge`):

```bash
gh workflow run publish.yml -f tag=edge
```

One-time setup — two repository secrets, from a Docker Hub **access token**
(Account Settings → Personal access tokens), never your password:

```bash
gh secret set DOCKERHUB_USERNAME
gh secret set DOCKERHUB_TOKEN
```

Building locally instead is fine too:

```bash
docker build -t youruser/fpl-buddy:0.1.0 .
docker push youruser/fpl-buddy:0.1.0
```

## 2. Run it

```bash
docker run -d --name fpl-buddy \
  --restart unless-stopped \
  -p 8080:8080 \
  --env-file .env \
  -v fpl-buddy-state:/data \
  youruser/fpl-buddy:0.1.0
```

Then check it:

```bash
curl localhost:8080/health
docker logs -f fpl-buddy
```

## Rules any host has to satisfy

These are not preferences. Break one and the thing silently stops working.

| Rule | Why |
|---|---|
| **Exactly one instance** | The scheduler runs in-process. Two instances propose twice and submit twice. |
| **Never scale to zero** | A stopped container has no scheduler, so nothing commits at the deadline. |
| **Stays up between gameweeks** | Same reason. This is a long-running service, not a cron job or a batch task. |
| **Durable state** | `STATE_DIR` (default `/data`) must survive restarts, or a pending proposal vanishes and never auto-commits. Mount a volume, or set `STATE_BACKEND=azure_table`. |
| **`PUBLIC_BASE_URL` is reachable from your phone** | Approval links are built from it. A wrong value produces links that don't open. |
| **HTTPS in front** | The signed token in the URL is the only credential. Terminate TLS at a proxy or a platform ingress. |

On a platform with scale-to-zero (Cloud Run, Container Apps), pin
`min=max=1` and CPU always allocated — or use [serverless.md](serverless.md),
which is the deployment built for exactly that case and lifts every rule in the
table above except the last three.

## Environment

Start from [`.env.example`](../.env.example), which documents every knob. The
short version of what actually matters:

| Variable | Notes |
|---|---|
| `FPL_ENTRY_ID` | Required. From the URL of your points page. |
| `FPL_EMAIL`, `FPL_PASSWORD` | The auth path that needs no human upkeep — see below. |
| `FPL_COOKIE_HEADER` | Fallback for networks where the login is blocked. |
| `LLM_PROVIDER` | `azure` or `google`. Decides which of the two rows below applies. |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_KEY` | `LLM_PROVIDER=azure`. Or `AZURE_OPENAI_AUTH=managed_identity` where the platform can provide one. |
| `GOOGLE_API_KEY`, `GOOGLE_MODEL`, `GOOGLE_SUMMARY_MODEL` | `LLM_PROVIDER=google`. One AI Studio key; the agent and the harvest use different models. |
| `APPROVAL_SECRET` | Signs approval links. `python -c "import secrets;print(secrets.token_urlsafe(32))"`. Rotating it kills every outstanding link. |
| `PUBLIC_BASE_URL` | Externally reachable base URL, no trailing slash. |
| `API_KEY` | Set it once the service is on a public URL: read endpoints then require `X-API-Key`. |
| `STATE_BACKEND`, `STATE_DIR` | `file` + a mounted volume, or `azure_table`. |
| `NOTIFY_CHANNEL`, `WEBHOOK_URL` / `SMTP_*` / `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` | `discord` posts a proposal with Approve/Amend/Reject buttons; see [below](#discord). |
| `DISCORD_HARVEST_CHANNEL_ID` | Sends the daily article digest somewhere other than the proposal channel. Empty shares one channel. |
| `FIXTURE_HORIZON_GAMEWEEKS` | How many gameweeks of fixtures the agent sees (default 5). One extra request per run; degrades to the current gameweek if it fails. |
| `KNOWLEDGE_SOURCES_FILE` | Path to the article-source YAML. Empty disables harvesting. Mount the file into the container. See [below](#harvesting-articles). |
| `KNOWLEDGE_HARVEST_HOUR`, `KNOWLEDGE_INDEX_DAYS`, `KNOWLEDGE_INDEX_LIMIT` | When the daily harvest runs, and how much of the archive reaches the brief. |
| `DRY_RUN` | Leave `true` until you have worked through [verify-payloads.md](verify-payloads.md). |

Never put secrets in a deployment manifest you commit. Where they go instead is
a choice with a price attached, and the two scripts here answer it differently:

- **Azure** (`infra/azure/deploy.sh`) uses Container Apps secrets, which are
  free.
- **GCP** (`infra/gcp/deploy.sh`) sets them as plain environment variables.
  Secret Manager bills **$0.06 per enabled version per month**, and the script
  added a version on every deploy — a bill that grew with the deploy count
  rather than with the deployment. The trade is that anyone with `run.viewer`
  on the project can read every value in the console. Fine for a personal
  project with one owner; change it back for anything shared, by restoring the
  `--set-secrets` wiring in git history.

## Authentication

FPL uses OAuth. Your session is a short-lived `access_token` plus a long-lived
`refresh_token`, both issued by PingOne.

**Set `FPL_EMAIL` and `FPL_PASSWORD`.** The service drives Premier League's own
login flow — an OAuth authorization-code exchange with PKCE against
`account.premierleague.com`, over a `curl_cffi` session that presents Chrome's
TLS fingerprint. The impersonation is the load-bearing part: the bot protection
reads the handshake, and a stock HTTP client is refused before it gets to send a
password. Prove it works from wherever you deploy:

```bash
docker exec fpl-buddy fpl-buddy login
```

Credentials are what make the deployment unattended. Sessions are obtained in
this order, so the cheap path is still the common one:

| Order | Path | When |
|---|---|---|
| 1 | Cached access token | Still live (they last **8 hours**). |
| 2 | Refresh | One request. Refresh tokens **rotate on every use**, so the cache holds the only live copy. |
| 3 | Password login | Refresh unavailable or rejected. Needs no prior state, so this is what turns a lost token into a working session instead of an alert. |
| 4 | `FPL_COOKIE_HEADER` | Last resort, for a network where the login itself is blocked. |

Two consequences:

- **A durable `STATE_DIR` is worth having, but is no longer critical.** It saves
  a full login per run. Losing it costs requests, not the deployment.
- **`FPL_PASSWORD` is a real secret in production.** Where each script puts it,
  and why, is in [Environment](#environment) above. Never commit it.

For the fallback header: log in at fantasy.premierleague.com in a browser →
DevTools → Network → any `/api/me/` request → Request Headers → copy the entire
`cookie` value. It must contain `access_token` and `refresh_token`.

Check state at any time, without touching the network:

```bash
docker exec fpl-buddy fpl-buddy token
docker exec fpl-buddy fpl-buddy verify
```

`verify` probes `/my-team/`, not `/me/` — `/me/` answers `200` for a session with
no usable access token at all, so it will happily tell you everything is fine
while the squad is unreadable.

## Discord

Set `NOTIFY_CHANNEL=discord` to get proposals as an embed with Approve / Amend /
Reject buttons, instead of (or alongside a separate) email or webhook. The
buttons call the exact same `Orchestrator` methods the web approval link and
the CLI do -- there is no separate Discord-only decision path.

The bot also reads (but never replies to) ordinary messages in that same
channel: anything you type there during the day is saved as a note and folded
into the next scheduled proposal's brief, then marked used so it doesn't repeat
into the following gameweek. There is no back-and-forth chat -- just somewhere
to drop a thought ("bench Vasquez, he's got a knock") whenever it occurs to
you. A 📝 reaction on your message confirms it was captured.

1. [discord.com/developers/applications](https://discord.com/developers/applications) →
   **New Application** → **Bot** → **Reset Token** → copy it into
   `DISCORD_BOT_TOKEN`.
2. Same page, **Bot** tab → **Privileged Gateway Intents** → turn on
   **MESSAGE CONTENT INTENT** and save. This is what lets the bot read the
   notes you type -- without it, the bot fails to connect at all (buttons and
   modals alone don't need it, but the notes feature does).
3. **OAuth2 → URL Generator** → scope `bot` → permissions **Send Messages**,
   **Embed Links**, **Read Message History**, **Add Reactions** → open the
   generated URL and add the bot to your server.
4. In Discord, enable **Developer Mode** (User Settings → Advanced), right
   click the channel you want proposals (and notes) in → **Copy Channel ID** →
   set `DISCORD_CHANNEL_ID`.
5. Optionally repeat for a second channel and set `DISCORD_HARVEST_CHANNEL_ID`,
   which the daily article digest goes to instead.

### Why two channels

The two messages want opposite things from you. A proposal is time-critical and
needs a decision inside the review window; the harvest digest is reading
material you get to whenever. Put them together and the digest arrives every
single morning while proposals arrive once a week — which trains you to swipe
the notification away, on the one channel where that is expensive.

`DISCORD_CHANNEL_ID` stays the channel the bot **reads** from, so notes you type
still have to go there. Leaving `DISCORD_HARVEST_CHANNEL_ID` empty shares one
channel for both, which is exactly what happened before this setting existed.

The bot is a persistent gateway connection living inside this same process
(same "one always-on replica" constraint as the scheduler -- see
[decisions.md](decisions.md)), not a second thing to deploy. If the container
restarts mid-approval-window, old buttons keep working: they're matched by a
pattern on the message's `custom_id`, not by anything held in memory. Notes
survive a restart too, for the same reason proposals do -- see `STATE_BACKEND`
above; a note captured just before a redeploy would otherwise silently never
reach the agent.

## Harvesting articles

A daily job collects FPL tips and team news from sources you list, summarises
each one, and stores it as markdown the agent can read while reasoning.

```bash
cp sources.example.yaml sources.yaml
export KNOWLEDGE_SOURCES_FILE=./sources.yaml
.venv/bin/fpl-buddy harvest --dry-run   # what would be collected, no writes
.venv/bin/fpl-buddy harvest             # collect, summarise, store
.venv/bin/fpl-buddy articles            # what's in the store now
```

In a container, mount the file and point at it:

```bash
docker run -d --name fpl-buddy --restart unless-stopped \
  -p 8080:8080 --env-file .env -v fpl-buddy-state:/data \
  -v "$PWD/sources.yaml:/app/sources.yaml:ro" \
  -e KNOWLEDGE_SOURCES_FILE=/app/sources.yaml \
  youruser/fpl-buddy:0.1.0
```

Notes go to `${STATE_DIR}/knowledge`, so the same durability requirement as
proposals applies — on a `STATE_BACKEND=azure_table` deployment there is no
durable filesystem, and harvesting would need a blob-backed store to survive
restarts. Set `KNOWLEDGE_HARVEST_HOUR` well before the propose window so a
proposal reasons over that morning's articles rather than yesterday's.

Each source declares how to find articles. Prefer a feed: it is one request, it
is a stable format, and it is the publisher telling you what is new. Listing
pages (`roots`) are the fallback and are crawled within a strict page budget —
`max_depth: 0` means the roots themselves, which is almost always what you want.

**Fetch backends.** Article pages are fetched by the first backend in
`KNOWLEDGE_FETCH_BACKENDS` that can handle them; each is skipped when
unavailable, so naming one you have not installed is harmless.

| Backend | Install | What it adds |
|---|---|---|
| `firecrawl` | `pip install -e '.[firecrawl]'` + `FIRECRAWL_API_KEY` | Renders JavaScript, gets past bot protection. **Metered**: 1 credit per page, 1000/month free. |
| `scrapling` | `pip install -e '.[scrapling]'` | Local, free, no browsers needed. Impersonates a browser's TLS fingerprint, which clears most `403`s. |
| `httpx` | built in | Always available. The reason neither of the above is required. |

Only *article pages* use these. Feeds, sitemaps, `robots.txt` and listing pages
always go over plain HTTP — they need no rendering, and routing them through a
metered service would spend roughly 150 credits a month achieving nothing.

Budget: a 26-article daily harvest is ~780 credits/month against a 1000 free
tier. `FIRECRAWL_CREDIT_RESERVE` (default 50) stops the harvester before it
spends the last of them, after which it falls through to the next backend.
Remaining credits are read from the API, not tallied locally.

`SCRAPLING_STEALTH=true` swaps Scrapling's plain fetcher for its browser-based
one, which needs `scrapling install` to download browsers first — several
hundred MB, so it is off by default and not in the image.

**Paywalls.** A freemium site returns `200` with the first part of the article
and a signup pitch; the rest is never sent to a logged-out client, so no crawler
and no headless browser can recover it. Those notes are stored with
`access: partial` and the agent is told they were cut off. If you hold a
subscription, point `cookie_env` at an environment variable containing your own
session cookie and full articles are stored instead — check the site's terms
first, since automated access to members-only content is often not permitted even
for members. There is no paywall circumvention in this codebase.

**Harvested text is untrusted.** It is fenced as data at the summariser, which
can only return a fixed schema; element ids are resolved from `bootstrap-static`
rather than taken from articles; and the brief and tools label it as third-party
opinion. See [decisions.md](decisions.md) for what that does and does not buy.

## First gameweek

1. `DRY_RUN=true`, `NOTIFY_CHANNEL=webhook`. Let a full cycle run and read what it
   proposes without it being able to act.
2. Work through [verify-payloads.md](verify-payloads.md) — diff the write payloads
   against a real browser capture.
3. Set `DRY_RUN=false` and do one live run **yourself**, watching, with hours of
   slack. Confirm in the FPL app that what landed is what you expected.
4. Only then leave `AUTO_COMMIT_ENABLED=true` to run unattended.

## Cost

Dominated by the always-on instance; everything else is rounding. One small
always-on container (0.5 vCPU / 1 GiB) runs roughly $25–35/month on a managed
container platform, or nothing at all on a machine you already pay for. The agent
itself is one run per gameweek — single-digit dollars per season on a mid-tier
model.

That instance does about twenty minutes of real work a month, which is what
[serverless.md](serverless.md) is for: the same image driven by a platform cron,
at roughly $0.50/month.
