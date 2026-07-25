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
| `NOTIFY_CHANNEL`, `WEBHOOK_URL` / `SMTP_*` | `webhook` into ntfy/Telegram/Slack is the easiest thing that reaches a phone. |
| `DRY_RUN` | Leave `true` until you have worked through [verify-payloads.md](verify-payloads.md). |

Pass secrets as your platform's secret references, not as plaintext in a
deployment manifest you commit.

## The cookie header

Premier League fronts login with bot protection that routinely returns `403` to
datacenter IPs. Plan on the cookie path anywhere other than your own machine:

1. Log in at fantasy.premierleague.com in a normal browser.
2. DevTools → Network → click any `/api/me/` request.
3. Request Headers → copy the entire `cookie` value.
4. Set it as `FPL_COOKIE_HEADER`. It must contain `pl_profile` and `sessionid`.

Cookies expire. When `/healthz` is fine but the propose job logs auth failures,
re-paste the header and restart. A calendar reminder before the first deadline of
each month is cheaper than a missed gameweek.

Verify from wherever it runs:

```bash
docker exec fpl-buddy fpl-buddy verify
```

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
