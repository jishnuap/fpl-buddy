# Deploying

There is no infrastructure-as-code in this repo. The deliverable is a container
image on Docker Hub plus the environment contract below; where you run it is
your call.

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
curl localhost:8080/healthz
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
`min=max=1` and CPU always allocated. If you would rather let the platform's
scheduler drive it — and pay nothing for idle — that means replacing
`scheduler.py` with authenticated job endpoints; see the note at the end of
[decisions.md](decisions.md).

## Environment

Start from [`.env.example`](../.env.example), which documents every knob. The
short version of what actually matters:

| Variable | Notes |
|---|---|
| `FPL_ENTRY_ID` | Required. From the URL of your points page. |
| `FPL_COOKIE_HEADER` | The realistic auth path in the cloud — see below. |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_KEY` | Or `AZURE_OPENAI_AUTH=managed_identity` where the platform can provide one. |
| `APPROVAL_SECRET` | Signs approval links. `python -c "import secrets;print(secrets.token_urlsafe(32))"`. Rotating it kills every outstanding link. |
| `PUBLIC_BASE_URL` | Externally reachable base URL, no trailing slash. |
| `API_KEY` | Set it once the service is on a public URL: read endpoints then require `X-API-Key`. |
| `STATE_BACKEND`, `STATE_DIR` | `file` + a mounted volume, or `azure_table`. |
| `NOTIFY_CHANNEL`, `WEBHOOK_URL` / `SMTP_*` / `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` | `discord` posts a proposal with Approve/Amend/Reject buttons; see [below](#discord). |
| `FIXTURE_HORIZON_GAMEWEEKS` | How many gameweeks of fixtures the agent sees (default 5). One extra request per run; degrades to the current gameweek if it fails. |
| `DRY_RUN` | Leave `true` until you have worked through [verify-payloads.md](verify-payloads.md). |

Pass secrets as your platform's secret references, not as plaintext in a
deployment manifest you commit.

## Authentication

FPL uses OAuth. Your session is a short-lived `access_token` plus a long-lived
`refresh_token`, both issued by PingOne and carried as cookies. Premier League's
bot protection returns `403` to datacenter IPs, so programmatic login is not
available anywhere but your own machine — the pasted header is the way in:

1. Log in at fantasy.premierleague.com in a normal browser.
2. DevTools → Network → click any `/api/me/` request.
3. Request Headers → copy the entire `cookie` value.
4. Set it as `FPL_COOKIE_HEADER`. It must contain `access_token` and
   `refresh_token`.

The lifetimes are what matter operationally:

| Token | Lives | Consequence |
|---|---|---|
| `access_token` | **8 hours** | Shorter than one gameweek cycle (propose T-36h → commit T-45m), so it is *always* refreshed at least once per cycle. |
| `refresh_token` | **~180 days** | You re-paste roughly twice a season, not twice a week. |

Refresh happens automatically before any request that needs it. Two things follow
that are easy to get wrong:

- **`STATE_DIR` must be durable.** Refreshed tokens are cached there, and the
  refresh token **rotates on every use** — the copy in your environment is spent
  the moment the first refresh succeeds. An ephemeral `STATE_DIR` gives you
  exactly one refresh per paste, and then the deadline job starts failing.
- **Prove the refresh before trusting a deadline to it:**

```bash
docker exec fpl-buddy fpl-buddy token --refresh
```

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

The bot is a persistent gateway connection living inside this same process
(same "one always-on replica" constraint as the scheduler -- see
[decisions.md](decisions.md)), not a second thing to deploy. If the container
restarts mid-approval-window, old buttons keep working: they're matched by a
pattern on the message's `custom_id`, not by anything held in memory. Notes
survive a restart too, for the same reason proposals do -- see `STATE_BACKEND`
above; a note captured just before a redeploy would otherwise silently never
reach the agent.

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
