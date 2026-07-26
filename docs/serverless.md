# Running it without an always-on container

The default deployment keeps one container alive forever because the scheduler
lives inside it. That instance is roughly 98% of the hosting cost for something
that does about twenty minutes of real work a month.

This is the other way to run it: two things that both scale to zero, driven by a
platform cron. Idle cost is a storage account and nothing else.

```bash
IMAGE=youruser/fpl-buddy:0.1.0 ./infra/azure/deploy.sh
PROJECT=my-project IMAGE=youruser/fpl-buddy:0.1.0 ./infra/gcp/deploy.sh
```

Both scripts read `.env` for configuration, upload `sources.yaml` if you have
one, and print how to verify the result. Re-running either is the redeploy path.

## The shape

| | What runs | When |
|---|---|---|
| **Job** | `fpl-buddy tick` | Every 10 minutes, from the platform's cron |
| **Service** | `fpl-buddy serve` with `SCHEDULER_ENABLED=false` | Only while you have the approval page open |
| **Volume** | Azure Files / GCS bucket at `/data` | Mounted by both |

One image, two deployments. `main.py` is unchanged for the always-on case —
`tick` is an alternative driver over the same logic, not a replacement, and both
derive their times from the same `schedule.plan_for()`.

## What a tick actually does

```
read the ledger                    # one small JSON file on /data
  nothing due?  -> exit            # the overwhelming majority of ticks
re-read the live deadline          # only when stale, or the window is close
  propose_at <= now < commit_at, and no proposal yet?  -> propose
  commit_at  <= now < deadline,  and one is pending?   -> commit
  past the harvest hour and not harvested today?       -> harvest
```

**Polling, not delayed tasks.** Enqueueing a task for the exact minute is more
precise and fails worse: one dropped enqueue and nothing commits, silently. A
missed poll costs ten minutes. It also needs no queue service to exist, and
deadlines move for international breaks and rescheduled fixtures anyway — so the
schedule has to be re-derived from `bootstrap-static` regardless, which is
exactly what each tick does.

**Idle ticks are cheap on purpose.** The cost argument only holds if doing
nothing is nearly free, so nothing above the "nothing due" exit imports the
agent stack or touches the network. The ledger caches the next deadline; the
live one is re-read every `TICK_ANCHOR_INTERVAL_HOURS` (default 6) while it is
far away, and every tick once the propose window is within an hour.

**Ticks do not overlap.** A propose run takes minutes and the platform starts
the next execution regardless, so the ledger carries a lease. It is cooperative,
not a distributed lock — the jobs behind it are individually idempotent, and the
lease exists to stop wasted work rather than to guarantee correctness.

## Timing

The tick interval is the resolution of the whole schedule. At `*/10`:

| | Configured | Actually fires |
|---|---|---|
| Propose | T-36h | between T-36h and T-35h50m |
| Commit | T-45m | between T-45m and T-35m |

`COMMIT_MINUTES_BEFORE_DEADLINE=45` leaves plenty of room for that. Do not shrink
it towards the tick interval. If you want tighter, lower `TICK_CRON` to `*/5` —
it is still comfortably inside both platforms' free grants.

## Why the volume is not optional

`STATE_DIR` holds four things, and one of them makes an ephemeral filesystem
fatal rather than inconvenient:

| | Consequence of losing it |
|---|---|
| `proposals/` | A pending proposal vanishes and never auto-commits |
| `notes.json` | Notes typed during the day never reach the agent |
| `knowledge/` | The article archive resets |
| **`fpl_cookies.json`** | **The FPL refresh token rotates on every use.** The copy in your environment is spent the moment the first refresh succeeds. Ephemeral state gives you exactly one refresh per paste, and then the deadline job starts failing. |

Note that `STATE_BACKEND=azure_table` does **not** get you out of this: the
cookie cache is on `state_dir` regardless of the backend. Since you need a real
filesystem either way, `STATE_BACKEND=file` on the mount is the simplest thing
that works, and it is what both scripts configure.

## Concurrent writes

Two processes now share that volume, so it is worth being precise about where
that is safe.

**Both scripts set `EXECUTE_ON_APPROVAL=false`.** This is the important one.
With it, approving records intent and the tick job submits at T-45m against
fresher data — which means the web service never calls FPL at all, so it never
refreshes the token. Every FPL write ends up in one process. Leave it on and two
processes can refresh at once, and since the refresh token rotates on use, the
loser's copy is dead. Turn it off only if you understand that.

**Azure Files** is SMB and handles the rest properly.

**Cloud Storage FUSE** does not provide file locking: when two writers replace
the same file, the last one wins and the other is lost. What survives that:

- Proposals are one whole file per id, written by one process at a time.
- The ledger is only ever written by the tick job, and only one runs at a time.
- `notes.json` is a read-modify-write of a single file. Nothing writes it in this
  deployment — see below — but if you add something that does, a note can be
  lost. It is one note, not the proposal.

## What you lose

**Discord buttons.** A gateway WebSocket keeps a container alive exactly like
the scheduler does, so `SCHEDULER_ENABLED=false` turns off both. Proposals still
arrive in the channel as the same embed, posted over plain HTTPS, with the
approval link in a field instead of on a button. Approve, amend and reject all
work from that page.

**Passive note capture.** The bot reading ordinary messages in the channel needs
the gateway too, so notes are only capturable from the CLI in this deployment.
Getting both back means registering a Discord interactions endpoint URL and
verifying its Ed25519 signatures — a real feature, and not attempted here.

If either matters more to you than the hosting cost, run the always-on
deployment in [deployment.md](deployment.md) instead. Nothing about it changed.

## Cost

An idle tick measures **~0.9s wall**, interpreter startup included. At `*/10`
that is 4,320 of them a month, about 65 minutes of wall time — roughly 2,000
vCPU-seconds at 0.5 vCPU, against a free grant of 180,000. Around 1%. The
propose and commit runs that actually do something are weekly and measured in
minutes, which does not move it.

The free grants — 180,000 vCPU-seconds and 360,000 GiB-seconds a month — are the
same on both platforms.

| | Azure | GCP |
|---|---|---|
| Compute | £0, inside the free grant | £0, inside the free tier |
| Storage | ~£0.40, Azure Files | ~£0.05, GCS |
| Scheduler | included in Jobs | Cloud Scheduler, 3 jobs free |
| Registry | **£0 on Docker Hub.** ACR Basic is ~£4/mo — do not use it for this | **£0 on Docker Hub** |
| Model | Azure OpenAI, per token, unchanged | unchanged |

Against roughly £20–28/month for one always-on 0.5 vCPU container. The registry
line is worth repeating: pushed to a managed registry instead of Docker Hub, it
becomes the single largest item on the bill.

## Verifying it

A deployment that scaled to zero with the scheduler still switched on looks
perfectly healthy and quietly never commits, so check the mode explicitly:

```bash
curl -s https://your-url/healthz     # expect "scheduler": "external"
```

Then run one tick by hand. "nothing due" is the correct answer when no deadline
is close, and it proves the path end to end:

```bash
az containerapp job start --name fpl-buddy-tick --resource-group fpl-buddy
gcloud run jobs execute fpl-buddy-tick --region europe-west2 --wait
```

Then prove the FPL session survives a refresh, because nothing else will tell
you it hasn't until a deadline passes without a submission. Both deploy scripts
print the exact command for your deployment.

Work through the [first gameweek checklist](deployment.md#first-gameweek) the
same way as for the always-on deployment. `DRY_RUN=true` until you have watched
a full cycle.
