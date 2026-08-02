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

# Read from ENV_FILE and set as ordinary environment variables on both the job
# and the service.
#
# This used to route the credentials through Secret Manager. It no longer does,
# and that is a deliberate trade: a new secret version was created on every
# deploy and every enabled version is billed monthly, so the bill grew with the
# deploy count rather than with the deployment. Env vars cost nothing.
#
# The price is that every value here -- the Discord bot token, the Azure OpenAI
# key, the approval secret, the FPL password -- is readable in plain text by
# anyone holding `run.viewer` on this project, in the console and in
# `gcloud run services describe`. That is an acceptable trade for a personal
# project owned by one person. It is not one to copy into a shared project.
#
# PUBLIC_BASE_URL is deliberately absent: it has to be the service's own URL,
# which this script discovers and sets for you.
ENV_KEYS=(
  FPL_ENTRY_ID TIMEZONE LOG_LEVEL DRY_RUN AUTO_COMMIT_ENABLED
  PROPOSE_HOURS_BEFORE_DEADLINE COMMIT_MINUTES_BEFORE_DEADLINE
  MAX_POINTS_HIT MIN_CAPTAIN_CONFIDENCE FIXTURE_HORIZON_GAMEWEEKS
  AZURE_OPENAI_ENDPOINT AZURE_OPENAI_DEPLOYMENT AZURE_OPENAI_API_VERSION
  NOTIFY_CHANNEL NOTIFY_HARVEST NOTIFY_ERRORS DISCORD_CHANNEL_ID
  DISCORD_HARVEST_CHANNEL_ID DISCORD_ERROR_CHANNEL_ID
  SMTP_HOST SMTP_PORT SMTP_FROM SMTP_TO
  KNOWLEDGE_HARVEST_HOUR KNOWLEDGE_INDEX_DAYS KNOWLEDGE_INDEX_LIMIT
  KNOWLEDGE_FETCH_BACKENDS FIRECRAWL_CREDIT_RESERVE
  TICK_ANCHOR_INTERVAL_HOURS FPL_LOGIN_IMPERSONATE
  FPL_EMAIL FPL_PASSWORD FPL_COOKIE_HEADER
  AZURE_OPENAI_API_KEY APPROVAL_SECRET API_KEY
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
  for candidate in "${ENV_KEYS[@]}"; do
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

load_env_file

# ------------------------------------------------------------------- project
say "Enabling the APIs this needs"
gcloud services enable \
  run.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com \
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

# -------------------------------------------------------------- environment
#
# Written to a YAML file rather than passed on the command line. `--set-env-vars`
# needs one delimiter that appears in no value, and there isn't one: a cookie
# header contains pipes, KNOWLEDGE_FETCH_BACKENDS contains commas, an email
# contains @. A file sidesteps the question, and keeps the values out of the
# process list while the deploy runs.
say "Environment"
ENV_FILE_YAML="$(mktemp -t fpl-buddy-env)"
# The values are credentials now that Secret Manager is out of the picture.
chmod 600 "$ENV_FILE_YAML"
trap 'rm -f "$ENV_FILE_YAML"' EXIT

# YAML single-quoted scalars take anything but a newline; the one escape is a
# doubled quote. None of ours contains a newline.
yaml_pair() { printf "%s: '%s'\n" "$1" "${2//\'/\'\'}"; }

{
  yaml_pair STATE_DIR /data
  yaml_pair STATE_BACKEND file
  for key in "${ENV_KEYS[@]}"; do
    value="$(value_of "$key")"
    [[ -n "$value" ]] || continue
    yaml_pair "$key" "$value"
  done
  [[ -f "$SOURCES_FILE" ]] && yaml_pair KNOWLEDGE_SOURCES_FILE /data/sources.yaml
} > "$ENV_FILE_YAML"

# gcloud refuses to replace a secret-backed variable with a literal in the same
# call -- "already been set with a different type" -- so anything left over from
# when this script used Secret Manager has to be cleared first. Both are no-ops
# on a fresh project and on every deploy after the first.
#
# Clearing is conditional on there actually being a secret reference to clear.
# An unconditional clear costs a revision on every deploy, and for the service
# that revision has no credentials in it -- which the app's own startup guard
# rejects ("NOTIFY_CHANNEL=discord needs both DISCORD_BOT_TOKEN and
# DISCORD_CHANNEL_ID"), so the deploy fails before it can set them.
has_secret_refs() {
  local kind="$1" name="$2"
  gcloud run "$kind" describe "$name" --region "$REGION" --project "$PROJECT" \
    --format='value(spec.template.spec.containers[0].env)' 2>/dev/null \
    | grep -q "secretKeyRef"
}

if has_secret_refs jobs "$JOB_NAME"; then
  say "Clearing the old Secret Manager references on $JOB_NAME"
  gcloud run jobs update "$JOB_NAME" --region "$REGION" --project "$PROJECT" \
    --clear-secrets --quiet --format=none
fi
if has_secret_refs services "$SERVICE_NAME"; then
  say "Clearing the old Secret Manager references on $SERVICE_NAME"
  # --no-traffic: this revision is credential-less by construction, so it must
  # not be allowed to serve. The next command in this script gives it the
  # values as literals and takes traffic back.
  gcloud run services update "$SERVICE_NAME" --region "$REGION" --project "$PROJECT" \
    --clear-secrets --no-traffic --quiet --format=none || true
fi

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
  "${VOLUME_ARGS[@]}" \
  --env-vars-file "$ENV_FILE_YAML" \
  --clear-secrets \
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
SERVICE_ENV_YAML="$(mktemp -t fpl-buddy-service-env)"
chmod 600 "$SERVICE_ENV_YAML"
trap 'rm -f "$ENV_FILE_YAML" "$SERVICE_ENV_YAML"' EXIT

deploy_service() {
  # SCHEDULER_ENABLED=false because the scheduler and the Discord gateway are
  # the two things that would keep this instance alive; the job owns the
  # schedule now. EXECUTE_ON_APPROVAL=false because approval then records
  # intent and the tick job submits it at T-45m against fresher data -- which
  # keeps every FPL write in one process, and the refresh token rotates on use.
  {
    cat "$ENV_FILE_YAML"
    yaml_pair PUBLIC_BASE_URL "$1"
    yaml_pair SCHEDULER_ENABLED false
    yaml_pair EXECUTE_ON_APPROVAL false
  } > "$SERVICE_ENV_YAML"

  gcloud run deploy "$SERVICE_NAME" \
    --image "$IMAGE" \
    --region "$REGION" --project "$PROJECT" \
    --service-account "$SA_EMAIL" \
    --port 8080 \
    --cpu 1 --memory 1Gi \
    --min-instances 0 --max-instances 1 \
    --allow-unauthenticated \
    "${VOLUME_ARGS[@]}" \
    --env-vars-file "$SERVICE_ENV_YAML" \
    --clear-secrets \
    --quiet
}

# --allow-unauthenticated is correct here and worth being explicit about: the
# approval link is opened from a phone with no Google identity, and the signed
# token in the URL is the credential. Set API_KEY to gate the read endpoints.
deploy_service "http://localhost:8080"
URL="$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" --project "$PROJECT" --format 'value(status.url)')"
deploy_service "$URL"

# The job is what actually posts the Discord notification (the service only
# serves the approval page it links to), so it needs the same real URL --
# otherwise every review link it sends is the http://localhost:8080 default,
# unusable from anywhere but this machine.
say "Pointing $JOB_NAME at the same approval URL"
gcloud run jobs update "$JOB_NAME" \
  --region "$REGION" --project "$PROJECT" \
  --update-env-vars "PUBLIC_BASE_URL=$URL" \
  --quiet

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
