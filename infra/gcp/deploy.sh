#!/usr/bin/env bash
#
# Deploy fpl-buddy to Cloud Run with nothing running between gameweeks.
#
#   PROJECT=my-project IMAGE=youruser/fpl-buddy:0.1.0 ./infra/gcp/deploy.sh
#
# Three things get created, from the same Docker Hub image:
#
#   * a Cloud Run *job* running `fpl-buddy tick`
#   * a Cloud Scheduler entry that runs that job on a cron
#   * a Cloud Run *service* serving the approval pages, min-instances 0
#
# Both compute resources mount the same GCS bucket at /data via Cloud Storage
# FUSE. That mount is not optional: the FPL refresh token rotates on every use
# and is cached there, so an ephemeral filesystem gives you exactly one refresh
# and then a dead session.
#
# One caveat that Azure Files does not have. Cloud Storage FUSE provides no
# file locking: when two writers replace the same file, the last one wins and
# the other is lost. This deployment is arranged so that does not bite --
# EXECUTE_ON_APPROVAL=false keeps every FPL write inside the tick job, and the
# tick job takes a lease so only one runs at a time -- but a note typed into
# Discord at the exact moment a proposal consumes the queue can still be lost.
# See docs/serverless.md.
#
# Idle cost is the bucket and nothing else: 4,320 ticks a month, most of them
# exiting in a couple of seconds, sit well inside the Cloud Run free tier
# (180,000 vCPU-seconds).

set -euo pipefail

# --------------------------------------------------------------------- config
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-europe-west2}"
SERVICE_NAME="${SERVICE_NAME:-fpl-buddy-web}"
JOB_NAME="${JOB_NAME:-fpl-buddy-tick}"
SCHEDULER_NAME="${SCHEDULER_NAME:-fpl-buddy-tick-schedule}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-fpl-buddy}"
ENV_FILE="${ENV_FILE:-.env}"
SOURCES_FILE="${SOURCES_FILE:-sources.yaml}"

# Every ten minutes. This is the resolution of the whole schedule: the commit
# job fires somewhere in the ten minutes after T-45m, which is why the default
# commit window sits 45 minutes out rather than five.
TICK_CRON="${TICK_CRON:-*/10 * * * *}"
TICK_TIMEZONE="${TICK_TIMEZONE:-Etc/UTC}"

# Bucket names are globally unique. cksum is POSIX, unlike shasum/sha1sum.
BUCKET="${BUCKET:-fpl-buddy-state-$(printf '%s' "$PROJECT" | cksum | cut -d' ' -f1)}"

: "${PROJECT:?Set PROJECT, or run: gcloud config set project <id>}"
: "${IMAGE:?Set IMAGE, e.g. IMAGE=youruser/fpl-buddy:0.1.0}"

SA_EMAIL="$SERVICE_ACCOUNT@$PROJECT.iam.gserviceaccount.com"

# Read from ENV_FILE. SECRET_KEYS go into Secret Manager and are mounted by
# reference; PLAIN_KEYS are set as ordinary environment variables and are
# visible to anyone with console access to the project.
#
# PUBLIC_BASE_URL is deliberately absent: it has to be the service's own URL,
# which this script discovers and sets for you.
PLAIN_KEYS=(
  FPL_ENTRY_ID TIMEZONE LOG_LEVEL DRY_RUN AUTO_COMMIT_ENABLED
  PROPOSE_HOURS_BEFORE_DEADLINE COMMIT_MINUTES_BEFORE_DEADLINE
  MAX_POINTS_HIT MIN_CAPTAIN_CONFIDENCE FIXTURE_HORIZON_GAMEWEEKS
  AZURE_OPENAI_ENDPOINT AZURE_OPENAI_DEPLOYMENT AZURE_OPENAI_API_VERSION
  NOTIFY_CHANNEL NOTIFY_HARVEST DISCORD_CHANNEL_ID DISCORD_HARVEST_CHANNEL_ID
  SMTP_HOST SMTP_PORT SMTP_FROM SMTP_TO
  KNOWLEDGE_HARVEST_HOUR KNOWLEDGE_INDEX_DAYS KNOWLEDGE_INDEX_LIMIT
  KNOWLEDGE_FETCH_BACKENDS FIRECRAWL_CREDIT_RESERVE
  TICK_ANCHOR_INTERVAL_HOURS
)
SECRET_KEYS=(
  FPL_COOKIE_HEADER AZURE_OPENAI_API_KEY APPROVAL_SECRET API_KEY
  DISCORD_BOT_TOKEN WEBHOOK_URL SMTP_PASSWORD FIRECRAWL_API_KEY FFS_COOKIE
)

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# --------------------------------------------------------------- env plumbing
#
# Values live in shell variables named VAL_<KEY> rather than an associative
# array, because macOS still ships bash 3.2 and `declare -A` does not exist
# there -- and this script is most likely to be run from a Mac.

known_key() {
  local candidate
  for candidate in "${PLAIN_KEYS[@]}" "${SECRET_KEYS[@]}"; do
    [[ "$candidate" == "$1" ]] && return 0
  done
  return 1
}

value_of() {
  local name="VAL_$1"
  printf '%s' "${!name:-}"
}

load_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "No $ENV_FILE found; deploying with defaults only." >&2
    return
  fi
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" != *=* ]] && continue
    key="$(printf '%s' "${line%%=*}" | tr -d '[:space:]')"
    value="${line#*=}"
    # Allowlisted only: a stray PROJECT= or REGION= in .env must not silently
    # redirect the deployment.
    known_key "$key" || continue
    # Strip one layer of surrounding quotes, the way python-dotenv does.
    if [[ "$value" =~ ^\"(.*)\"$ ]]; then value="${BASH_REMATCH[1]}"; fi
    if [[ "$value" =~ ^\'(.*)\'$ ]]; then value="${BASH_REMATCH[1]}"; fi
    [[ -n "$value" ]] && printf -v "VAL_$key" '%s' "$value"
  done < "$ENV_FILE"
}

secret_id() { printf 'fpl-buddy-%s' "$(printf '%s' "$1" | tr '[:upper:]_' '[:lower:]-')"; }

load_env_file

# ------------------------------------------------------------------- project
say "Enabling the APIs this needs"
gcloud services enable \
  run.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com \
  --project "$PROJECT" --quiet

say "Service account $SA_EMAIL"
gcloud iam service-accounts create "$SERVICE_ACCOUNT" \
  --display-name "fpl-buddy" --project "$PROJECT" --quiet 2>/dev/null || true

say "Bucket gs://$BUCKET"
gcloud storage buckets create "gs://$BUCKET" \
  --project "$PROJECT" --location "$REGION" \
  --uniform-bucket-level-access --quiet 2>/dev/null || true

# storage.objectUser, not objectViewer: the mount is read-write, and the
# refresh-token cache is the reason why.
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$SA_EMAIL" \
  --role roles/storage.objectUser \
  --project "$PROJECT" --quiet --format=none

if [[ -f "$SOURCES_FILE" ]]; then
  say "Uploading $SOURCES_FILE to the bucket"
  gcloud storage cp "$SOURCES_FILE" "gs://$BUCKET/sources.yaml" \
    --project "$PROJECT" --quiet
fi

# ------------------------------------------------------------------ secrets
say "Secrets"
SECRET_ARGS=()
for key in "${SECRET_KEYS[@]}"; do
  value="$(value_of "$key")"
  [[ -n "$value" ]] || continue
  id="$(secret_id "$key")"
  gcloud secrets create "$id" --replication-policy automatic \
    --project "$PROJECT" --quiet 2>/dev/null || true
  # A new version every deploy. Old ones stay readable, so a bad value is one
  # `gcloud secrets versions` command away from being rolled back.
  printf '%s' "$value" | gcloud secrets versions add "$id" \
    --data-file=- --project "$PROJECT" --quiet --format=none
  gcloud secrets add-iam-policy-binding "$id" \
    --member "serviceAccount:$SA_EMAIL" \
    --role roles/secretmanager.secretAccessor \
    --project "$PROJECT" --quiet --format=none
  SECRET_ARGS+=("--set-secrets=$key=$id:latest")
done

# gcloud puts --set-env-vars and --update-env-vars in a mutually exclusive
# group, so everything has to arrive as one flag. The ^|^ prefix switches the
# separator from a comma to a pipe, which is what lets a value like
# KNOWLEDGE_FETCH_BACKENDS=firecrawl,scrapling,httpx through intact. (A value
# containing a literal pipe would still break; none of ours does.)
ENV_PAIRS="STATE_DIR=/data|STATE_BACKEND=file"
for key in "${PLAIN_KEYS[@]}"; do
  value="$(value_of "$key")"
  [[ -n "$value" ]] || continue
  ENV_PAIRS="$ENV_PAIRS|$key=$value"
done
[[ -f "$SOURCES_FILE" ]] && \
  ENV_PAIRS="$ENV_PAIRS|KNOWLEDGE_SOURCES_FILE=/data/sources.yaml"

# GCS volume mounts need the second-generation execution environment.
VOLUME_ARGS=(
  "--add-volume=name=state,type=cloud-storage,bucket=$BUCKET"
  "--add-volume-mount=volume=state,mount-path=/data"
  "--execution-environment=gen2"
)

# ------------------------------------------------------------------- the job
say "Job $JOB_NAME"
gcloud run jobs deploy "$JOB_NAME" \
  --image "$IMAGE" \
  --region "$REGION" --project "$PROJECT" \
  --service-account "$SA_EMAIL" \
  --command fpl-buddy --args tick \
  --cpu 1 --memory 1Gi \
  --max-retries 0 \
  --task-timeout 1800s \
  --tasks 1 \
  "${VOLUME_ARGS[@]}" "${SECRET_ARGS[@]}" \
  --set-env-vars "^|^$ENV_PAIRS" \
  --quiet

say "Schedule $SCHEDULER_NAME ('$TICK_CRON' $TICK_TIMEZONE)"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:$SA_EMAIL" \
  --role roles/run.invoker --quiet --format=none

RUN_URI="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/$JOB_NAME:run"
SCHEDULER_ARGS=(
  --location "$REGION" --project "$PROJECT"
  --schedule "$TICK_CRON" --time-zone "$TICK_TIMEZONE"
  --uri "$RUN_URI" --http-method POST
  # OAuth rather than OIDC: the target is a Google API, not our own service.
  --oauth-service-account-email "$SA_EMAIL"
)
if gcloud scheduler jobs describe "$SCHEDULER_NAME" \
     --location "$REGION" --project "$PROJECT" --quiet >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$SCHEDULER_NAME" "${SCHEDULER_ARGS[@]}" --quiet
else
  gcloud scheduler jobs create http "$SCHEDULER_NAME" "${SCHEDULER_ARGS[@]}" --quiet
fi

# --------------------------------------------------------------- the service
# PUBLIC_BASE_URL has to be the service's own URL, which does not exist until
# the service does -- so it is deployed once, asked its address, and updated.
say "Service $SERVICE_NAME (scales to zero)"
deploy_service() {
  # SCHEDULER_ENABLED=false because the scheduler and the Discord gateway are
  # the two things that would keep this instance alive; the job owns the
  # schedule now. EXECUTE_ON_APPROVAL=false because approval then records
  # intent and the tick job submits it at T-45m against fresher data -- which
  # keeps every FPL write in one process, and the refresh token rotates on use.
  gcloud run deploy "$SERVICE_NAME" \
    --image "$IMAGE" \
    --region "$REGION" --project "$PROJECT" \
    --service-account "$SA_EMAIL" \
    --port 8080 \
    --cpu 1 --memory 1Gi \
    --min-instances 0 --max-instances 1 \
    --allow-unauthenticated \
    "${VOLUME_ARGS[@]}" "${SECRET_ARGS[@]}" \
    --set-env-vars "^|^$ENV_PAIRS|PUBLIC_BASE_URL=$1|SCHEDULER_ENABLED=false|EXECUTE_ON_APPROVAL=false" \
    --quiet
}

# --allow-unauthenticated is correct here and worth being explicit about: the
# approval link is opened from a phone with no Google identity, and the signed
# token in the URL is the credential. Set API_KEY to gate the read endpoints.
deploy_service "http://localhost:8080"
URL="$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" --project "$PROJECT" --format 'value(status.url)')"
deploy_service "$URL"

# ---------------------------------------------------------------------- done
cat <<DONE

$(printf '\033[1mDeployed.\033[0m')

  Approval pages   $URL
  Health           $URL/healthz
  Tick schedule    $TICK_CRON ($TICK_TIMEZONE)

Check it before trusting a deadline to it:

  curl -s $URL/healthz                   # expect "scheduler": "external"
  gcloud run jobs execute $JOB_NAME --region $REGION --project $PROJECT --wait
  gcloud run jobs executions list --job $JOB_NAME --region $REGION --project $PROJECT

A first tick reporting "nothing due" is the correct answer when no deadline is
close, and it proves the whole path works. Then prove the FPL session survives
a refresh, because nothing else will tell you it hasn't until a deadline:

  gcloud run jobs update $JOB_NAME --region $REGION --project $PROJECT \\
    --args token,--refresh --quiet
  gcloud run jobs execute $JOB_NAME --region $REGION --project $PROJECT --wait
  gcloud run jobs update $JOB_NAME --region $REGION --project $PROJECT \\
    --args tick --quiet
DONE
