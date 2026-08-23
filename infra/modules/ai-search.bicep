// ---------------------------------------------------------------------------
// Azure AI Search — Knowledge indexes for agentic RAG
// Provides semantic search across equipment, maintenance, store, and alarm domains.
// ---------------------------------------------------------------------------

@description('Azure region for the resource')
param location string

@description('Name for the Azure AI Search resource')
param name string

@description('SKU for Azure AI Search')
@allowed(['basic', 'standard', 'standard2', 'standard3'])
param sku string = 'basic'

@description('Resource tags')
param tags object = {}

resource searchService 'Microsoft.Search/searchServices@2024-03-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: sku
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    hostingMode: 'default'
    partitionCount: 1
    replicaCount: 1
    publicNetworkAccess: 'enabled'
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
    semanticSearch: 'free'
  }
}

output searchServiceId string = searchService.id
output searchServiceName string = searchService.name
output searchServiceEndpoint string = 'https://${searchService.name}.search.windows.net'
