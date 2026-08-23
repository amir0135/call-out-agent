// ---------------------------------------------------------------------------
// Azure OpenAI — GPT-4o-mini deployment for SOP reasoning
//
// Why mini: the SOP loop only picks the next step from a small enumerated
// list (see SOPBranch.match) and produces a short utterance. Mini matches
// 4o quality for this task and shaves ~300–600ms off TTFT, which is the
// single biggest lever on perceived turn-taking latency.
// ---------------------------------------------------------------------------

param location string
param name string
param deploymentName string = 'gpt-5-mini'
@description('Actual model behind the deployment. May differ from deploymentName so app config stays stable when a model is deprecated for new deployments.')
param modelName string = 'gpt-5-mini'
param modelVersion string = '2025-08-07'
@description('Deployment SKU. Some regions (e.g. westeurope) only offer GlobalStandard for gpt-4o-mini, not Standard.')
param deploymentSkuName string = 'Standard'
param deploymentCapacity int = 30
@description('Deploy the gpt-realtime speech-to-speech model (used when STREAMING_ENGINE=realtime).')
param deployRealtime bool = true
param realtimeDeploymentName string = 'gpt-realtime'
param realtimeModelVersion string = '2025-08-28'
param realtimeCapacity int = 8
param tags object = {}

resource openAiAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: name
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: name
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true  // Enforce Entra-only auth
  }
}

resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAiAccount
  name: deploymentName
  sku: {
    name: deploymentSkuName
    capacity: deploymentCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
  }
}

resource realtimeDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = if (deployRealtime) {
  parent: openAiAccount
  name: realtimeDeploymentName
  dependsOn: [deployment] // deployments on one account must be serialized
  sku: {
    name: 'GlobalStandard' // gpt-realtime is GlobalStandard-only
    capacity: realtimeCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-realtime'
      version: realtimeModelVersion
    }
  }
}

output endpoint string = openAiAccount.properties.endpoint
output accountId string = openAiAccount.id
output accountName string = openAiAccount.name
output deploymentName string = deployment.name
