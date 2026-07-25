// Azure Container Apps deployment for fpl-buddy.
//
// Shape worth understanding before you change it:
//   * minReplicas == maxReplicas == 1. The scheduler lives inside the app, so a
//     second replica would propose twice and commit twice.
//   * No scale-to-zero. A scaled-to-zero app has no scheduler, so nothing
//     commits at the deadline.
//   * Table Storage holds proposals, because a revision restart must not lose the
//     pending proposal that is supposed to auto-commit.
//   * The app gets a system-assigned identity and the Cognitive Services OpenAI
//     User role, so AZURE_OPENAI_AUTH=managed_identity needs no key at all.

@description('Base name; resource names derive from it.')
param name string = 'fpl-buddy'

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Container image, e.g. myacr.azurecr.io/fpl-buddy:2025-09-01.')
param image string

@description('Your FPL entry id.')
param fplEntryId int

@description('Azure OpenAI endpoint, e.g. https://my-aoai.openai.azure.com')
param azureOpenAiEndpoint string

@description('Model deployment name inside that resource.')
param azureOpenAiDeployment string = 'gpt-4.1'

param azureOpenAiApiVersion string = '2024-10-21'

@description('Name of the Azure OpenAI / AI Foundry account in THIS resource group. Leave empty to skip the role assignment and use a key instead.')
param azureOpenAiAccountName string = ''

@description('Cookie header copied from a browser. Required in practice: FPL bot protection rejects datacenter IPs.')
@secure()
param fplCookieHeader string = ''

@description('Signs approval links. Generate with: python -c "import secrets;print(secrets.token_urlsafe(32))"')
@secure()
param approvalSecret string

@description('Protects the read endpoints once the app is on a public URL.')
@secure()
param apiKey string = ''

@description('Webhook that receives proposal notifications (Telegram bot, ntfy, Slack, Shortcuts...).')
@secure()
param webhookUrl string = ''

@allowed(['none', 'log', 'smtp', 'webhook'])
param notifyChannel string = 'webhook'

@description('Keep true until the write payloads have been diffed against a real browser capture.')
param dryRun bool = true

param autoCommitEnabled bool = true
param maxPointsHit int = 0
param proposeHoursBeforeDeadline int = 36
param commitMinutesBeforeDeadline int = 45
param timeZone string = 'Asia/Kolkata'

@description('Registry login server. Leave empty for a public image.')
param registryServer string = ''
@secure()
param registryPassword string = ''
param registryUsername string = ''

var shortName = take(replace(toLower(name), '-', ''), 12)
var storageName = take('${shortName}${uniqueString(resourceGroup().id)}', 24)
// Built-in role: Cognitive Services OpenAI User.
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-logs'
  location: location
  properties: {
    retentionInDays: 30
    sku: { name: 'PerGB2018' }
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource proposalsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'fplproposals'
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${name}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        traffic: [ { latestRevision: true, weight: 100 } ]
      }
      registries: empty(registryServer) ? [] : [
        {
          server: registryServer
          username: registryUsername
          passwordSecretRef: 'registry-password'
        }
      ]
      secrets: concat(
        [
          { name: 'approval-secret', value: approvalSecret }
          { name: 'table-connection', value: storageConnectionString }
          { name: 'fpl-cookie-header', value: fplCookieHeader }
          { name: 'api-key', value: apiKey }
          { name: 'webhook-url', value: webhookUrl }
        ],
        empty(registryServer) ? [] : [ { name: 'registry-password', value: registryPassword } ]
      )
    }
    template: {
      containers: [
        {
          name: 'app'
          image: image
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'FPL_ENTRY_ID', value: string(fplEntryId) }
            { name: 'FPL_COOKIE_HEADER', secretRef: 'fpl-cookie-header' }
            { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: azureOpenAiDeployment }
            { name: 'AZURE_OPENAI_API_VERSION', value: azureOpenAiApiVersion }
            { name: 'AZURE_OPENAI_AUTH', value: empty(azureOpenAiAccountName) ? 'api_key' : 'managed_identity' }
            { name: 'STATE_BACKEND', value: 'azure_table' }
            { name: 'AZURE_TABLE_CONNECTION_STRING', secretRef: 'table-connection' }
            { name: 'AZURE_TABLE_NAME', value: proposalsTable.name }
            { name: 'STATE_DIR', value: '/data' }
            { name: 'DRY_RUN', value: string(dryRun) }
            { name: 'AUTO_COMMIT_ENABLED', value: string(autoCommitEnabled) }
            { name: 'MAX_POINTS_HIT', value: string(maxPointsHit) }
            { name: 'PROPOSE_HOURS_BEFORE_DEADLINE', value: string(proposeHoursBeforeDeadline) }
            { name: 'COMMIT_MINUTES_BEFORE_DEADLINE', value: string(commitMinutesBeforeDeadline) }
            { name: 'TIMEZONE', value: timeZone }
            { name: 'NOTIFY_CHANNEL', value: notifyChannel }
            { name: 'WEBHOOK_URL', secretRef: 'webhook-url' }
            { name: 'APPROVAL_SECRET', secretRef: 'approval-secret' }
            { name: 'API_KEY', secretRef: 'api-key' }
            // Set after the first deploy, once the FQDN is known -- approval
            // links are built from this and must be externally reachable.
            { name: 'PUBLIC_BASE_URL', value: 'https://${name}.${env.properties.defaultDomain}' }
            { name: 'PORT', value: '8080' }
            { name: 'LOG_LEVEL', value: 'INFO' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8080 }
              initialDelaySeconds: 20
              periodSeconds: 60
            }
          ]
        }
      ]
      // One replica, always on. The scheduler is in-process.
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

resource openAiAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = if (!empty(azureOpenAiAccountName)) {
  name: azureOpenAiAccountName
}

resource openAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(azureOpenAiAccountName)) {
  name: guid(resourceGroup().id, name, openAiUserRoleId)
  scope: openAiAccount
  properties: {
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
}

output fqdn string = app.properties.configuration.ingress.fqdn
output publicBaseUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output principalId string = app.identity.principalId
output storageAccount string = storage.name
