#!/usr/bin/env bash
# Build the image, push it to ACR, and deploy the Container App.
#
# Idempotent: re-run it to ship a new image. It does not rotate APPROVAL_SECRET
# (that would invalidate every outstanding approval link), and it never flips
# DRY_RUN for you -- pass DRY_RUN=false explicitly when you mean it.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-fpl-buddy-rg}"
LOCATION="${LOCATION:-centralindia}"
APP_NAME="${APP_NAME:-fpl-buddy}"
ACR_NAME="${ACR_NAME:-}"
TAG="${TAG:-$(date +%Y%m%d-%H%M%S)}"
DRY_RUN="${DRY_RUN:-true}"

: "${FPL_ENTRY_ID:?set FPL_ENTRY_ID}"
: "${AZURE_OPENAI_ENDPOINT:?set AZURE_OPENAI_ENDPOINT}"
: "${APPROVAL_SECRET:?set APPROVAL_SECRET (python -c \"import secrets;print(secrets.token_urlsafe(32))\")}"

# Optional: AZURE_OPENAI_ACCOUNT_NAME (enables managed identity), FPL_COOKIE_HEADER,
# WEBHOOK_URL, API_KEY.
AZURE_OPENAI_ACCOUNT_NAME="${AZURE_OPENAI_ACCOUNT_NAME:-}"
FPL_COOKIE_HEADER="${FPL_COOKIE_HEADER:-}"
WEBHOOK_URL="${WEBHOOK_URL:-}"
API_KEY="${API_KEY:-}"
NOTIFY_CHANNEL="${NOTIFY_CHANNEL:-log}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"

echo "==> Resource group $RESOURCE_GROUP ($LOCATION)"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

if [[ -z "$ACR_NAME" ]]; then
  ACR_NAME="acr$(echo "$RESOURCE_GROUP" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]' | cut -c1-16)"
fi

if ! az acr show --name "$ACR_NAME" --output none 2>/dev/null; then
  echo "==> Creating container registry $ACR_NAME"
  az acr create --resource-group "$RESOURCE_GROUP" --name "$ACR_NAME" \
    --sku Basic --admin-enabled true --output none
fi

echo "==> Building $ACR_NAME.azurecr.io/$APP_NAME:$TAG in ACR"
az acr build --registry "$ACR_NAME" --image "$APP_NAME:$TAG" "$root" --output none

REGISTRY_SERVER="$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)"
REGISTRY_USERNAME="$(az acr credential show --name "$ACR_NAME" --query username -o tsv)"
REGISTRY_PASSWORD="$(az acr credential show --name "$ACR_NAME" --query 'passwords[0].value' -o tsv)"

echo "==> Deploying (dryRun=$DRY_RUN)"
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$here/main.bicep" \
  --parameters \
      name="$APP_NAME" \
      location="$LOCATION" \
      image="$REGISTRY_SERVER/$APP_NAME:$TAG" \
      fplEntryId="$FPL_ENTRY_ID" \
      azureOpenAiEndpoint="$AZURE_OPENAI_ENDPOINT" \
      azureOpenAiAccountName="$AZURE_OPENAI_ACCOUNT_NAME" \
      fplCookieHeader="$FPL_COOKIE_HEADER" \
      approvalSecret="$APPROVAL_SECRET" \
      apiKey="$API_KEY" \
      webhookUrl="$WEBHOOK_URL" \
      notifyChannel="$NOTIFY_CHANNEL" \
      dryRun="$DRY_RUN" \
      registryServer="$REGISTRY_SERVER" \
      registryUsername="$REGISTRY_USERNAME" \
      registryPassword="$REGISTRY_PASSWORD" \
  --query 'properties.outputs' \
  --output json

echo
echo "==> Done. Check it:"
echo "    curl https://\$(az containerapp show -g $RESOURCE_GROUP -n $APP_NAME --query properties.configuration.ingress.fqdn -o tsv)/healthz"
echo "    az containerapp logs show -g $RESOURCE_GROUP -n $APP_NAME --follow"
if [[ "$DRY_RUN" == "true" ]]; then
  echo
  echo "DRY_RUN is on: no writes will reach FPL. Diff the payloads against a real"
  echo "browser capture before deploying with DRY_RUN=false."
fi
