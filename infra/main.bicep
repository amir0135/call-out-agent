// ---------------------------------------------------------------------------
// Call-Out Agent PoC — Main Bicep Template
// Orchestrates all infrastructure modules with managed identity and RBAC.
// ---------------------------------------------------------------------------

targetScope = 'resourceGroup'

@description('Primary location for all resources')
param location string = resourceGroup().location

@description('Environment name used for resource naming')
param environmentName string = 'callout-poc'

@description('Azure OpenAI model deployment name')
param openAiDeploymentName string = 'gpt-5-mini'

@description('Actual model behind the deployment (gpt-4o-mini AND gpt-4.1-mini are in deprecating state — blocked for NEW deployments as of 2026-07; gpt-5-mini deprecates 2027-02)')
param openAiModelName string = 'gpt-5-mini'

@description('Azure OpenAI model version')
param openAiModelVersion string = '2025-08-07'

@description('ACS source phone number (provisioned manually)')
param acsPhoneNumber string = ''

@description('Existing ACS resource name (provisioned manually via Portal)')
param acsResourceName string = 'contoso-acs'

// NOTE ON REGION / LATENCY:
// ACS data location is FIXED at creation and cannot be moved. This resource
// lives in the US, so call media (and the PSTN hop) routes via the US even if
// the rest of the stack is deployed to Europe. To genuinely reduce latency for
// EU callers you must create a NEW ACS resource with a European data location
// and provision a new phone number, then point acsEndpoint/acsResourceName at
// it. Deploy Speech in the same European region via `speechLocation` below so
// TTS/STT synthesis co-locates with that ACS resource.
@description('Existing ACS endpoint (provisioned manually via Portal)')
param acsEndpoint string = 'https://contoso-acs.unitedstates.communication.azure.com'

@description('Location override for Cosmos DB (use when primary region has capacity issues)')
param cosmosLocation string = ''

@description('Location override for Azure AI Speech. Place near your callers (e.g. westeurope for EU) to cut TTS/STT first-byte latency. Defaults to the primary location.')
param speechLocation string = ''

@description('Location override for Azure AI Search (use when primary region lacks capacity for the chosen SKU). Search latency is not on the voice path.')
param searchLocation string = ''

@description('Location override for the Container Apps environment + apps (use when primary region reports AKSCapacityHeavyUsage). Adds a small network hop to Speech/OpenAI; remove once primary region has capacity.')
param containerAppsLocation string = ''

@description('OpenAI deployment SKU. Use GlobalStandard in regions (e.g. westeurope) that do not offer Standard for gpt-4o-mini.')
param openAiDeploymentSku string = 'Standard'

@description('Run agent + orchestrator in rule-based mode (no Azure OpenAI at runtime).')
param localMode string = 'false'

@description('TTS voice for the agent.')
param speechVoice string = 'en-US-JennyNeural'

@description('Enable bidirectional media streaming (low-latency voice path).')
param streamingMode string = 'false'

@description('Speech end-of-utterance segmentation timeout (ms) for the streaming path.')
param streamingSegmentationMs string = '150'

@description('Pause (ms) before the greeting so the caller can say hello first.')
param streamingGreetingDelayMs string = '1000'

@description('Speech recognition language (e.g. en-GB).')
param acsSpeechLanguage string = 'en-US'

@description('ACS connection string. Set for connection-string auth; leave empty to use managed identity.')
@secure()
param acsConnectionString string = ''

@description('Deploy Azure Cache for Redis (lowest-latency session store). Set false where it is unavailable/retired; the orchestrator then uses the Cosmos-backed session store — horizontal scale is preserved either way.')
param deployRedis string = 'true'

@description('Max orchestrator replicas. Sized for 200k alarms/month with burst headroom; requires a shared session store (Redis or Cosmos), which this template always provides.')
param orchestratorMaxReplicas string = '50'

@description('Customer webhook that receives terminal call results (JSON POST). Empty = disabled; consumers can poll GET /api/alarm-status instead.')
param outcomeWebhookUrl string = ''

@description('HMAC-SHA256 secret for signing outcome webhook payloads (X-Callout-Signature header). Empty = unsigned.')
@secure()
param outcomeWebhookSecret string = ''

@description('API key required on POST /api/alarm-intent and the status endpoints (X-API-Key header). Empty = open (dev only).')
@secure()
param orchestratorApiKey string = ''

@description('Voice engine for the media-streaming path: chained (Speech STT/TTS), realtime (GPT speech-to-speech) or voicelive (managed Voice Live API).')
param streamingEngine string = 'chained'

@description('Voice for the realtime speech-to-speech engine.')
param realtimeVoice string = 'alloy'

@description('Voice Live endpoint (Foundry/AI Services or Speech resource). Empty = use the Speech account deployed by this template.')
param voiceLiveEndpoint string = ''

@description('Voice Live managed model (no deployment/quota needed): gpt-realtime, gpt-realtime-mini, ...')
param voiceLiveModel string = 'gpt-realtime'

@description('Voice for the Voice Live engine — any Azure neural voice.')
param voiceLiveVoice string = 'en-GB-OllieMultilingualNeural'

@description('Container image for the orchestrator')
param orchestratorImage string = ''

@description('Container image for the agent')
param agentImage string = ''

// ---------------------------------------------------------------------------
// Naming convention
// ---------------------------------------------------------------------------
var resourceToken = uniqueString(resourceGroup().id, environmentName)
// Effective Speech region: override via speechLocation, else the primary location.
var effectiveSpeechLocation = !empty(speechLocation) ? speechLocation : location
// The orchestrator's own public URL — ACS posts call webhooks here and the
// media-streaming WebSocket is derived from it. Built from the app name +
// the Container Apps environment domain (self-reference would be circular).
var orchestratorAppName = 'ca-orchestrator-${resourceToken}'
var callbackBaseUrl = 'https://${orchestratorAppName}.${containerAppEnv.outputs.defaultDomain}'
var tags = {
  environment: environmentName
  project: 'call-out-agent'
  'azd-env-name': environmentName
}

// ---------------------------------------------------------------------------
// Modules
// ---------------------------------------------------------------------------

module appInsights 'modules/app-insights.bicep' = {
  name: 'app-insights'
  params: {
    location: location
    name: 'appi-${resourceToken}'
    logAnalyticsName: 'log-${resourceToken}'
    tags: tags
  }
}

module acr 'modules/acr.bicep' = {
  name: 'acr'
  params: {
    location: location
    name: 'acr${resourceToken}'
    tags: tags
  }
}

// ACS is NOT provisioned by Bicep — it was created manually via the Azure Portal
// with phone number provisioning. Reference it via parameters instead.
// Resource: ${acsResourceName} → ${acsEndpoint}

module cosmosDb 'modules/cosmosdb.bicep' = {
  name: 'cosmosdb'
  params: {
    location: !empty(cosmosLocation) ? cosmosLocation : location
    name: 'cosmos-${resourceToken}'
    tags: tags
  }
}

module openAi 'modules/openai.bicep' = {
  name: 'openai'
  params: {
    location: location
    name: 'oai-${resourceToken}'
    deploymentName: openAiDeploymentName
    modelName: openAiModelName
    modelVersion: openAiModelVersion
    deploymentSkuName: openAiDeploymentSku
    tags: tags
  }
}

module speech 'modules/speech.bicep' = {
  name: 'speech'
  params: {
    location: effectiveSpeechLocation
    name: 'speech-${resourceToken}'
    tags: tags
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    name: 'st${resourceToken}'
    tags: tags
  }
}

module aiSearch 'modules/ai-search.bicep' = {
  name: 'ai-search'
  params: {
    location: !empty(searchLocation) ? searchLocation : location
    name: 'search-${resourceToken}'
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Azure Cache for Redis — shared session store (multi-replica safety)
// ---------------------------------------------------------------------------
module redis 'modules/redis.bicep' = if (deployRedis == 'true') {
  name: 'redis'
  params: {
    location: location
    name: 'redis-${resourceToken}'
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Service Bus — burst-resilient alarm intake queue
// ---------------------------------------------------------------------------
module serviceBus 'modules/servicebus.bicep' = {
  name: 'servicebus'
  params: {
    location: location
    name: 'sb-${resourceToken}'
    queueName: 'alarm-intake'
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Container Apps Environment (shared by agent + orchestrator)
// ---------------------------------------------------------------------------
module containerAppEnv 'modules/container-app-env.bicep' = {
  name: 'container-app-env'
  params: {
    location: !empty(containerAppsLocation) ? containerAppsLocation : location
    name: 'cae-${resourceToken}'
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Agent Container App — SOP reasoning with agentic RAG
// ---------------------------------------------------------------------------
module agentApp 'modules/container-app.bicep' = {
  name: 'agent-app'
  params: {
    location: !empty(containerAppsLocation) ? containerAppsLocation : location
    managedEnvironmentId: containerAppEnv.outputs.environmentId
    appName: 'ca-agent-${resourceToken}'
    containerName: 'agent'
    acrLoginServer: acr.outputs.loginServer
    containerImage: agentImage
    targetPort: 8080
    tags: union(tags, { 'azd-service-name': 'agent' })
    envVars: [
      { name: 'LOCAL_MODE', value: localMode }
      { name: 'RAG_ENABLED', value: 'true' }
      { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.outputs.endpoint }
      { name: 'AZURE_OPENAI_DEPLOYMENT', value: openAiDeploymentName }
      { name: 'AZURE_AI_SEARCH_ENDPOINT', value: aiSearch.outputs.searchServiceEndpoint }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.outputs.connectionString }
    ]
  }
}

// ---------------------------------------------------------------------------
// Orchestrator Container App — call automation
// ---------------------------------------------------------------------------
module containerApp 'modules/container-app.bicep' = {
  name: 'container-app'
  params: {
    location: !empty(containerAppsLocation) ? containerAppsLocation : location
    managedEnvironmentId: containerAppEnv.outputs.environmentId
    appName: 'ca-orchestrator-${resourceToken}'
    containerName: 'orchestrator'
    acrLoginServer: acr.outputs.loginServer
    containerImage: orchestratorImage
    tags: union(tags, { 'azd-service-name': 'orchestrator' })
    serviceBusNamespace: serviceBus.outputs.namespace
    serviceBusQueueName: serviceBus.outputs.queueName
    serviceBusMessagesPerReplica: 20
    // Horizontal scale is safe with either shared session store: Redis when
    // deployed, otherwise the Cosmos call_sessions container.
    maxReplicas: int(orchestratorMaxReplicas)
    // Real-time audio streaming (Speech SDK threads + 20ms frame pacing + WS)
    // needs real CPU; 0.5 vCPU starves the event loop and breaks the audio.
    cpuCores: '2.0'
    memorySize: '4Gi'
    envVars: [
      { name: 'ACS_ENDPOINT', value: acsEndpoint }
      { name: 'ACS_PHONE_NUMBER', value: acsPhoneNumber }
      { name: 'ACS_CONNECTION_STRING', value: acsConnectionString }
      { name: 'COSMOS_ENDPOINT', value: cosmosDb.outputs.endpoint }
      { name: 'FOUNDRY_AGENT_ENDPOINT', value: 'https://${agentApp.outputs.fqdn}' }
      { name: 'SPEECH_REGION', value: effectiveSpeechLocation }
      // --- Streaming voice path (low-latency, EU-co-located) -------------
      { name: 'LOCAL_MODE', value: localMode }
      { name: 'STREAMING_MODE', value: streamingMode }
      { name: 'STREAMING_SEGMENTATION_MS', value: streamingSegmentationMs }
      { name: 'STREAMING_GREETING_DELAY_MS', value: streamingGreetingDelayMs }
      { name: 'STREAMING_BARGE_IN', value: 'false' }
      { name: 'SPEECH_VOICE', value: speechVoice }
      { name: 'SPEECH_RESOURCE_ID', value: speech.outputs.accountId }
      { name: 'COGNITIVE_SERVICES_ENDPOINT', value: '' }
      { name: 'ACS_SPEECH_LANGUAGE', value: acsSpeechLanguage }
      { name: 'CALLBACK_BASE_URL', value: callbackBaseUrl }
      // --- Realtime (GPT-4o speech-to-speech) engine, switchable ---------
      { name: 'STREAMING_ENGINE', value: streamingEngine }
      { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.outputs.endpoint }
      { name: 'REALTIME_DEPLOYMENT', value: 'gpt-realtime' }
      { name: 'REALTIME_VOICE', value: realtimeVoice }
      // --- Voice Live engine (STREAMING_ENGINE=voicelive) ----------------
      // Managed speech-to-speech: gpt-realtime brain + Azure voices +
      // semantic VAD. Needs a Foundry (AI Services) or Speech resource
      // endpoint; defaults to the Speech account (Voice Live supported,
      // minus agent-service/BYOM). Override via azd env VOICELIVE_ENDPOINT
      // to point at a Foundry resource for full features.
      { name: 'VOICELIVE_ENDPOINT', value: !empty(voiceLiveEndpoint) ? voiceLiveEndpoint : speech.outputs.endpoint }
      { name: 'VOICELIVE_MODEL', value: voiceLiveModel }
      { name: 'VOICELIVE_VOICE', value: voiceLiveVoice }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.outputs.connectionString }
      { name: 'AZURE_AI_SEARCH_ENDPOINT', value: aiSearch.outputs.searchServiceEndpoint }
      { name: 'RAG_ENABLED', value: 'true' }
      // --- Shared-state stores (required for maxReplicas > 1) -----------
      // Redis connection string for session store. ``rediss://`` enforces TLS.
      // When Redis is not deployed the Cosmos-backed session store below
      // takes over (REDIS_URL empty → SESSION_COSMOS_CONTAINER wins).
      { name: 'REDIS_URL', value: deployRedis == 'true' ? 'rediss://:${redis.outputs.primaryKey}@${redis.outputs.hostName}:${redis.outputs.sslPort}/0' : '' }
      // Cosmos-backed session store — multi-replica safe without Redis.
      { name: 'SESSION_COSMOS_CONTAINER', value: 'call_sessions' }
      // Cosmos container name selects CosmosRetryStore in the orchestrator.
      { name: 'RETRY_STATE_COSMOS_CONTAINER', value: 'retry_state' }
      // --- Integration surface -------------------------------------------
      // Terminal call results are POSTed here (signed when the secret is set).
      { name: 'OUTCOME_WEBHOOK_URL', value: outcomeWebhookUrl }
      { name: 'OUTCOME_WEBHOOK_SECRET', value: outcomeWebhookSecret }
      // X-API-Key guard on alarm intake + status endpoints.
      { name: 'ORCHESTRATOR_API_KEY', value: orchestratorApiKey }
      // --- Service Bus (burst-resilient alarm intake) -------------------
      // Always on: POST /api/alarm-intent returns 202 immediately and a
      // background consumer drains the queue. KEDA scales replicas on
      // queue depth.
      { name: 'SERVICEBUS_NAMESPACE', value: serviceBus.outputs.namespace }
      { name: 'SERVICEBUS_QUEUE_NAME', value: serviceBus.outputs.queueName }
      // --- ACS recognize tuning (calm voice, fast reaction, no interrupting) ---
      // Open-ended prompts: keep near the ACS ~1s floor so the reaction
      // after the caller stops is snappy without clipping free-form answers.
      { name: 'ACS_END_SILENCE_TIMEOUT', value: '1.2' }
      // Yes/no SOP steps via per-step override (1s = ACS minimum).
      { name: 'ACS_END_SILENCE_TIMEOUT_FAST', value: '1.0' }
      { name: 'ACS_INITIAL_SILENCE_TIMEOUT', value: '5.0' }
      // Barge-in OFF: prevents the agent talking over the caller.
      { name: 'ACS_INTERRUPT_PROMPT', value: 'false' }
      // DTMF fallback so callers always have a path forward when STT misfires.
      { name: 'ACS_ENABLE_DTMF_FALLBACK', value: 'true' }
      // SSML prosody rate for TTS. '-5%' gives a calmer, more human cadence;
      // the play path also adds sentence pauses and a slightly lower pitch.
      { name: 'SPEECH_RATE', value: '-5%' }
    ]
  }
}

// ---------------------------------------------------------------------------
// RBAC role assignments (separate module to avoid BCP120)
// ---------------------------------------------------------------------------
module rbac 'modules/rbac.bicep' = {
  name: 'rbac-assignments'
  params: {
    orchestratorPrincipalId: containerApp.outputs.principalId
    agentPrincipalId: agentApp.outputs.principalId
    cosmosAccountName: cosmosDb.outputs.accountName
    storageAccountId: storage.outputs.accountId
    openAiAccountId: openAi.outputs.accountId
    acrRegistryId: acr.outputs.registryId
    aiSearchServiceId: aiSearch.outputs.searchServiceId
    serviceBusNamespaceId: serviceBus.outputs.namespaceId
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output AZURE_ACS_ENDPOINT string = acsEndpoint
output AZURE_AGENT_APP_URL string = agentApp.outputs.fqdn
output AZURE_COSMOS_ENDPOINT string = cosmosDb.outputs.endpoint
output AZURE_OPENAI_ENDPOINT string = openAi.outputs.endpoint
output AZURE_SPEECH_REGION string = effectiveSpeechLocation
output AZURE_STORAGE_ACCOUNT string = storage.outputs.accountName
output AZURE_CONTAINER_REGISTRY_NAME string = acr.outputs.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.outputs.loginServer
output AZURE_CONTAINER_APP_URL string = containerApp.outputs.fqdn
output AZURE_APPLICATION_INSIGHTS_CONNECTION_STRING string = appInsights.outputs.connectionString
output AZURE_AI_PROJECT_ENDPOINT string = openAi.outputs.endpoint
output AZURE_AI_SEARCH_ENDPOINT string = aiSearch.outputs.searchServiceEndpoint
output AZURE_AI_SEARCH_NAME string = aiSearch.outputs.searchServiceName

// azd service-to-resource mapping
output SERVICE_AGENT_RESOURCE_NAME string = agentApp.outputs.appName
output SERVICE_ORCHESTRATOR_RESOURCE_NAME string = containerApp.outputs.appName
