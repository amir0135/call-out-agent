// ---------------------------------------------------------------------------
// RBAC Role Assignments for Call-Out Agent
// Assigns managed identity permissions for both orchestrator and agent.
// Deployed as a separate module so principalId inputs are resolved.
// ---------------------------------------------------------------------------

@description('Principal ID of the orchestrator managed identity')
param orchestratorPrincipalId string

@description('Principal ID of the agent managed identity')
param agentPrincipalId string

@description('Cosmos DB account name')
param cosmosAccountName string

@description('Storage account resource ID')
param storageAccountId string

@description('OpenAI account resource ID')
param openAiAccountId string

@description('ACR resource ID')
param acrRegistryId string

@description('AI Search service resource ID')
param aiSearchServiceId string

@description('Service Bus namespace resource ID')
param serviceBusNamespaceId string

// ---------------------------------------------------------------------------
// Role definition IDs
// ---------------------------------------------------------------------------
var storageBlobContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var searchIndexDataReaderRoleId = '1407120a-92aa-4202-b7e9-c0e197c71c8f'
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'
// Azure Service Bus Data Owner — needed for both send (producer) and
// receive/complete (consumer). Splitting into Sender + Receiver is
// possible but adds two assignments; for a single managed identity
// that does both, Owner is the simplest least-privilege choice.
var serviceBusDataOwnerRoleId = '090c5cfd-751d-490a-894a-3ce6f1109419'

// ---------------------------------------------------------------------------
// Orchestrator RBAC
// ---------------------------------------------------------------------------

// Cosmos DB Data Contributor
resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: cosmosAccountName
}

resource cosmosRoleAssignment 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(orchestratorPrincipalId, cosmosDataContributorRoleId, cosmosAccountName)
  properties: {
    roleDefinitionId: resourceId('Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions', cosmosAccountName, cosmosDataContributorRoleId)
    principalId: orchestratorPrincipalId
    scope: cosmosAccount.id
  }
}

// Storage Blob Data Contributor — orchestrator
resource storageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(orchestratorPrincipalId, storageBlobContributorRoleId, storageAccountId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobContributorRoleId)
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Cognitive Services User — orchestrator (for Speech)
resource orchestratorCogRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(orchestratorPrincipalId, cognitiveServicesUserRoleId, openAiAccountId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ACR Pull — orchestrator
resource orchestratorAcrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(orchestratorPrincipalId, acrPullRoleId, acrRegistryId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Search Index Data Reader — orchestrator
resource orchestratorSearchRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(orchestratorPrincipalId, searchIndexDataReaderRoleId, aiSearchServiceId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataReaderRoleId)
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Service Bus Data Owner — orchestrator (send + receive + complete)
resource orchestratorServiceBusRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(orchestratorPrincipalId, serviceBusDataOwnerRoleId, serviceBusNamespaceId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataOwnerRoleId)
    principalId: orchestratorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Agent RBAC
// ---------------------------------------------------------------------------

// Cognitive Services User — agent (for Azure OpenAI)
resource agentCogRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(agentPrincipalId, cognitiveServicesUserRoleId, openAiAccountId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Search Index Data Reader — agent (for agentic RAG)
resource agentSearchRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(agentPrincipalId, searchIndexDataReaderRoleId, aiSearchServiceId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataReaderRoleId)
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ACR Pull — agent
resource agentAcrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(agentPrincipalId, acrPullRoleId, acrRegistryId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}
