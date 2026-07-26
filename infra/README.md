# Deploy scripts

Two scripts, one for each cloud, both deploying the scale-to-zero shape
described in [docs/serverless.md](../docs/serverless.md): a cron job running
`fpl-buddy tick` plus a web service that idles at zero instances.

```bash
IMAGE=youruser/fpl-buddy:0.1.0 ./infra/azure/deploy.sh
PROJECT=my-project IMAGE=youruser/fpl-buddy:0.1.0 ./infra/gcp/deploy.sh
```

Each reads `.env` for configuration, uploads `sources.yaml` if present, and
prints how to verify the result. Re-running is the redeploy path.

Neither is infrastructure-as-code, deliberately — see the note in
[decisions.md](../docs/decisions.md). They are `az` and `gcloud` commands in the
order you would run them by hand, so there is no template DSL and no provider
API version to keep in sync. The tradeoff is that they do not track drift or
tear anything down; `az group delete` and `gcloud run services delete` are the
undo.

## What you need first

An image on Docker Hub, and the CLI for whichever cloud you are using, logged
in. `make publish TAG=v0.1.0` builds and pushes the image from CI.

## Knobs

Both scripts take overrides from the environment. The ones worth knowing:

| | Default | |
|---|---|---|
| `IMAGE` | *required* | Docker Hub image and tag |
| `TICK_CRON` | `*/10 * * * *` | Also the resolution of the whole schedule |
| `ENV_FILE` | `.env` | Where configuration is read from |
| `SOURCES_FILE` | `sources.yaml` | Uploaded to the volume if it exists |
| `LOCATION` / `REGION` | `uksouth` / `europe-west2` | |

Azure additionally takes `RESOURCE_GROUP`, `ENVIRONMENT`, `APP_NAME`,
`JOB_NAME`, `STORAGE_ACCOUNT`; GCP takes `PROJECT`, `SERVICE_NAME`, `JOB_NAME`,
`BUCKET`, `SERVICE_ACCOUNT`.

Only keys the scripts recognise are read out of `.env` — a stray `IMAGE=` or
`PROJECT=` in there cannot silently redirect a deployment somewhere else.

Secrets go to Container Apps secrets on Azure and Secret Manager on GCP, and are
referenced by name rather than inlined, so they do not show up in
`az containerapp show` or the Cloud Run console.
