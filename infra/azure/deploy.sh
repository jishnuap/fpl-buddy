#!/usr/bin/env bash
#
# Deploy fpl-buddy to Azure Container Apps with nothing running between
# gameweeks.
#
#   IMAGE=youruser/fpl-buddy:0.1.0 ./infra/azure/deploy.sh
#
# Two things get created, from the same Docker Hub image:
#
#   * a Container Apps *job*, on a cron, running `fpl-buddy tick`
#   * a Container *app* serving the approval pages, min-replicas 0
#
# Both mount the same Azure Files share at /data. That mount is not optional:
# the FPL refresh token rotates on every use and is cached there, so an
# ephemeral filesystem gives you exactly one refresh and then a dead session.
#
# Re-running this is the redeploy path -- each resource is created if missing
# and updated in place otherwise.
#
# Idle cost is the storage account and nothing else: a tick that finds nothing
# due exits in a couple of seconds, and 4,320 of those a month sit inside the
# Container Apps free grant (180,000 vCPU-seconds) several times over.

set -euo pipefail

# --------------------------------------------------------------------- config
RESOURCE_GROUP="${RESOURCE_GROUP:-fpl-buddy}"
LOCATION="${LOCATION:-uksouth}"
ENVIRONMENT="${ENVIRONMENT:-fpl-buddy-env}"
APP_NAME="${APP_NAME:-fpl-buddy-web}"
JOB_NAME="${JOB_NAME:-fpl-buddy-tick}"
SHARE_NAME="${SHARE_NAME:-state}"
STORAGE_MOUNT="${STORAGE_MOUNT:-fpl-buddy-state}"
ENV_FILE="${ENV_FILE:-.env}"
SOURCES_FILE="${SOURCES_FILE:-sources.yaml}"

# Every ten minutes. This is the resolution of the whole schedule: the commit
# job fires somewhere in the ten minutes after T-45m, which is why the default
# commit window sits 45 minutes out rather than five. Cron here is UTC.
TICK_CRON="${TICK_CRON:-*/10 * * * *}"

# Storage account names are globally unique, lowercase alphanumeric, 3-24
# chars. cksum is POSIX, unlike shasum/sha1sum which differ across platforms.
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-fplbuddy$(printf '%s' "$RESOURCE_GROUP$LOCATION" | cksum | cut -d' ' -f1)}"

: "${IMAGE:?Set IMAGE, e.g. IMAGE=youruser/fpl-buddy:0.1.0}"

# Read from ENV_FILE and passed through to both containers. Anything in
# SECRET_KEYS becomes a Container Apps secret referenced by name, so values do
# not appear in `az containerapp show` output.
#
# PUBLIC_BASE_URL is deliberately absent: it has to be the app's own FQDN,
# which this script discovers and sets for you.
PLAIN_KEYS=(
  FPL_ENTRY_ID TIMEZONE LOG_LEVEL DRY_RUN AUTO_COMMIT_ENABLED
  PROPOSE_HOURS_BEFORE_DEADLINE COMMIT_MINUTES_BEFORE_DEADLINE
  MAX_POINTS_HIT MIN_CAPTAIN_CONFIDENCE FIXTURE_HORIZON_GAMEWEEKS
  AZURE_OPENAI_ENDPOINT AZURE_OPENAI_DEPLOYMENT AZURE_OPENAI_API_VERSION
  NOTIFY_CHANNEL DISCORD_CHANNEL_ID SMTP_HOST SMTP_PORT SMTP_FROM SMTP_TO
  KNOWLEDGE_HARVEST_HOUR KNOWLEDGE_INDEX_DAYS KNOWLEDGE_INDEX_LIMIT
  KNOWLEDGE_FETCH_BACKENDS FIRECRAWL_CREDIT_RESERVE
  TICK_ANCHOR_INTERVAL_HOURS FPL_LOGIN_IMPERSONATE
)
SECRET_KEYS=(
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
    # Allowlisted only: a stray IMAGE= or REGION= in .env must not silently
    # redirect the deployment.
    known_key "$key" || continue
    # Strip one layer of surrounding quotes, the way python-dotenv does.
    if [[ "$value" =~ ^\"(.*)\"$ ]]; then value="${BASH_REMATCH[1]}"; fi
    if [[ "$value" =~ ^\'(.*)\'$ ]]; then value="${BASH_REMATCH[1]}"; fi
    [[ -n "$value" ]] && printf -v "VAL_$key" '%s' "$value"
  done < "$ENV_FILE"
}

secret_name() { printf '%s' "$1" | tr '[:upper:]_' '[:lower:]-'; }

# YAML single-quoted scalars need internal quotes doubled. A cookie header is
# full of '=' and ';' and will find any gap here.
#
# The quote character goes through a variable: writing the replacement inline
# as \'\' leaves the backslashes in the output on bash 3.2.
yaml_quote() {
  local q="'" value="$1"
  value="${value//$q/$q$q}"
  printf "'%s'" "$value"
}

# `secrets:` with nothing under it is not the same as no secrets at all, so
# this emits the key only when there is something to put in it.
emit_secret_block() {
  local indent="$1" key name value out=""
  for key in "${SECRET_KEYS[@]}"; do
    value="$(value_of "$key")"
    [[ -n "$value" ]] || continue
    name="$(secret_name "$key")"
    out+="$(printf '%s  - name: %s\n%s    value: %s' \
      "$indent" "$name" "$indent" "$(yaml_quote "$value")")"$'\n'
  done
  [[ -n "$out" ]] && printf '%ssecrets:\n%s' "$indent" "$out"
  return 0
}

emit_env() {
  local indent="$1" key name value
  printf '%s- name: STATE_DIR\n%s  value: /data\n' "$indent" "$indent"
  printf '%s- name: STATE_BACKEND\n%s  value: file\n' "$indent" "$indent"
  for key in "${PLAIN_KEYS[@]}"; do
    value="$(value_of "$key")"
    [[ -n "$value" ]] || continue
    printf '%s- name: %s\n%s  value: %s\n' \
      "$indent" "$key" "$indent" "$(yaml_quote "$value")"
  done
  for key in "${SECRET_KEYS[@]}"; do
    [[ -n "$(value_of "$key")" ]] || continue
    name="$(secret_name "$key")"
    printf '%s- name: %s\n%s  secretRef: %s\n' "$indent" "$key" "$indent" "$name"
  done
  if [[ -f "$SOURCES_FILE" ]]; then
    printf '%s- name: KNOWLEDGE_SOURCES_FILE\n%s  value: /data/sources.yaml\n' \
      "$indent" "$indent"
  fi
}

exists() { "$@" --output none 2>/dev/null; }

load_env_file

# ------------------------------------------------------------------ resources
say "Resource group $RESOURCE_GROUP in $LOCATION"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

say "Storage account $STORAGE_ACCOUNT and file share $SHARE_NAME"
az storage account create \
  --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" --sku Standard_LRS --kind StorageV2 --output none
STORAGE_KEY="$(az storage account keys list \
  --account-name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
  --query '[0].value' --output tsv)"
az storage share-rm create \
  --name "$SHARE_NAME" --storage-account "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" --quota 1 --output none

if [[ -f "$SOURCES_FILE" ]]; then
  say "Uploading $SOURCES_FILE to the share"
  az storage file upload \
    --account-name "$STORAGE_ACCOUNT" --account-key "$STORAGE_KEY" \
    --share-name "$SHARE_NAME" --source "$SOURCES_FILE" --path sources.yaml \
    --output none
fi

say "Container Apps environment $ENVIRONMENT"
az containerapp env create \
  --name "$ENVIRONMENT" --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" --output none
az containerapp env storage set \
  --name "$ENVIRONMENT" --resource-group "$RESOURCE_GROUP" \
  --storage-name "$STORAGE_MOUNT" \
  --azure-file-account-name "$STORAGE_ACCOUNT" \
  --azure-file-account-key "$STORAGE_KEY" \
  --azure-file-share-name "$SHARE_NAME" \
  --access-mode ReadWrite --output none

ENVIRONMENT_ID="$(az containerapp env show \
  --name "$ENVIRONMENT" --resource-group "$RESOURCE_GROUP" \
  --query id --output tsv)"

# Secret values land in these files, so keep them out of a world-readable
# directory and remove them on any exit, successful or not.
WORKDIR="$(mktemp -d)"
chmod 700 "$WORKDIR"
trap 'rm -rf "$WORKDIR"' EXIT

# ------------------------------------------------------------------- the job
say "Job $JOB_NAME on cron '$TICK_CRON' (UTC)"
cat > "$WORKDIR/job.yaml" <<YAML
properties:
  environmentId: $ENVIRONMENT_ID
  configuration:
    triggerType: Schedule
    replicaTimeout: 1800
    replicaRetryLimit: 0
    scheduleTriggerConfig:
      cronExpression: "$TICK_CRON"
      parallelism: 1
      replicaCompletionCount: 1
$(emit_secret_block '    ')
  template:
    containers:
      - name: tick
        image: $IMAGE
        command: ["fpl-buddy"]
        args: ["tick"]
        resources:
          cpu: 0.5
          memory: 1Gi
        env:
$(emit_env '          ')
        volumeMounts:
          - volumeName: state
            mountPath: /data
    volumes:
      - name: state
        storageType: AzureFile
        storageName: $STORAGE_MOUNT
YAML

if exists az containerapp job show --name "$JOB_NAME" --resource-group "$RESOURCE_GROUP"; then
  az containerapp job update --name "$JOB_NAME" --resource-group "$RESOURCE_GROUP" \
    --yaml "$WORKDIR/job.yaml" --output none
else
  az containerapp job create --name "$JOB_NAME" --resource-group "$RESOURCE_GROUP" \
    --yaml "$WORKDIR/job.yaml" --output none
fi

# ------------------------------------------------------------------- the app
# PUBLIC_BASE_URL has to be the app's own FQDN, which does not exist until the
# app does -- so it is deployed once, asked its address, and deployed again.
say "App $APP_NAME (scales to zero)"
deploy_app() {
  cat > "$WORKDIR/app.yaml" <<YAML
properties:
  environmentId: $ENVIRONMENT_ID
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: true
      targetPort: 8080
      transport: auto
      allowInsecure: false
$(emit_secret_block '    ')
  template:
    containers:
      - name: web
        image: $IMAGE
        resources:
          cpu: 0.5
          memory: 1Gi
        env:
$(emit_env '          ')
          - name: PUBLIC_BASE_URL
            value: "$1"
          # The scheduler and the Discord gateway are the two things that would
          # keep this replica alive. The cron job owns the schedule now.
          - name: SCHEDULER_ENABLED
            value: "false"
          # Approval records intent; the tick job submits it at T-45m against
          # fresher data. This keeps every FPL write in one process -- the
          # refresh token rotates on use, and two processes refreshing at once
          # invalidate the session for both.
          - name: EXECUTE_ON_APPROVAL
            value: "false"
        volumeMounts:
          - volumeName: state
            mountPath: /data
    scale:
      minReplicas: 0
      maxReplicas: 1
    volumes:
      - name: state
        storageType: AzureFile
        storageName: $STORAGE_MOUNT
YAML
  if exists az containerapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP"; then
    az containerapp update --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
      --yaml "$WORKDIR/app.yaml" --output none
  else
    az containerapp create --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
      --yaml "$WORKDIR/app.yaml" --output none
  fi
}

deploy_app "http://localhost:8080"
FQDN="$(az containerapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn --output tsv)"
deploy_app "https://$FQDN"

# ---------------------------------------------------------------------- done
cat <<DONE

$(printf '\033[1mDeployed.\033[0m')

  Approval pages   https://$FQDN
  Health           https://$FQDN/healthz
  Tick schedule    $TICK_CRON (UTC)

Check it before trusting a deadline to it:

  curl -s https://$FQDN/healthz          # expect "scheduler": "external"
  az containerapp job start --name $JOB_NAME --resource-group $RESOURCE_GROUP
  az containerapp job execution list --name $JOB_NAME --resource-group $RESOURCE_GROUP -o table

A first tick reporting "nothing due" is the correct answer when no deadline is
close, and it proves the whole path works. Then prove the FPL session survives
a refresh, because nothing else will tell you it hasn't until a deadline:

  az containerapp exec --name $APP_NAME --resource-group $RESOURCE_GROUP \\
    --command "fpl-buddy token --refresh"
DONE
