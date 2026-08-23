// ---------------------------------------------------------------------------
// Azure Storage Account — call recordings (Blob Storage)
// ---------------------------------------------------------------------------

param location string
param name string
param tags object = {}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: false  // Enforce Entra-only auth
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource recordingsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'recordings'
  properties: {
    publicAccess: 'None'
  }
}

output accountName string = storageAccount.name
output accountId string = storageAccount.id
output blobEndpoint string = storageAccount.properties.primaryEndpoints.blob
