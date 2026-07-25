# Deploying to Azure Container Apps

## Prerequisites

- An Azure subscription, `az` CLI, and Docker (only if you build locally —
  `deploy.sh` uses `az acr build`, so Docker is optional).
- An Azure OpenAI / AI Foundry resource with a chat deployment.
- Your FPL entry id, and a cookie header from your browser (see below).

## One command

```bash
export FPL_ENTRY_ID=1234567
export AZURE_OPENAI_ENDPOINT=https://my-aoai.openai.azure.com
export AZURE_OPENAI_ACCOUNT_NAME=my-aoai        # enables managed identity
export APPROVAL_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
export FPL_COOKIE_HEADER='pl_profile=...; sessionid=...'
export NOTIFY_CHANNEL=webhook
export WEBHOOK_URL=https://ntfy.sh/my-private-topic
./infra/deploy.sh
```

`DRY_RUN` defaults to `true`. Nothing reaches FPL until you deploy again with
`DRY_RUN=false`, and you should not do that before working through
[verify-payloads.md](verify-payloads.md).

## Why the shape is what it is

| Choice | Reason |
|---|---|
| `minReplicas: 1`, `maxReplicas: 1` | The scheduler runs in-process. Two replicas would propose twice and submit twice. |
| No scale-to-zero | A scaled-to-zero app has no scheduler, so nothing commits at the deadline. |
| Table Storage for proposals | A revision restart must not lose the pending proposal that is due to auto-commit. |
| System-assigned identity + Cognitive Services OpenAI User | `AZURE_OPENAI_AUTH=managed_identity` then needs no key in the environment. |
| External ingress | The approval link has to open on your phone. The signed token is the only credential; set `API_KEY` to close the read endpoints. |

## The cookie header

Premier League fronts login with bot protection that routinely returns `403` to
datacenter IPs — which is exactly what a Container App is. Plan on the cookie
path in production:

1. Log in at fantasy.premierleague.com in a normal browser.
2. DevTools → Network → click any `/api/me/` request.
3. Request Headers → copy the entire `cookie` value.
4. Set it as `FPL_COOKIE_HEADER`. It must contain `pl_profile` and `sessionid`.

Cookies expire. When `/healthz` is fine but the propose job logs auth failures,
refresh the header:

```bash
az containerapp secret set -g fpl-buddy-rg -n fpl-buddy \
  --secrets fpl-cookie-header="pl_profile=...; sessionid=..."
az containerapp revision restart -g fpl-buddy-rg -n fpl-buddy
```

A calendar reminder to re-paste it before the first deadline of each month is
cheaper than a missed gameweek.

## After the first deploy

```bash
FQDN=$(az containerapp show -g fpl-buddy-rg -n fpl-buddy \
  --query properties.configuration.ingress.fqdn -o tsv)
curl "https://$FQDN/healthz"
az containerapp logs show -g fpl-buddy-rg -n fpl-buddy --follow
```

`PUBLIC_BASE_URL` is set by the template to the app's own FQDN, so approval links
work without a second pass. If you put a custom domain in front, update it —
links are built from that value and a wrong one produces links that don't open.

## Cost

Roughly: one always-on 0.5 vCPU / 1 GiB Container App, a Standard_LRS storage
account with kilobytes in it, a Log Analytics workspace at 30-day retention, and
one agent run per gameweek (a handful of model calls). The always-on replica
dominates; everything else is rounding.
