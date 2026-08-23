// ---------------------------------------------------------------------------
// Azure Container Registry
// ---------------------------------------------------------------------------

param location string
param name string
param tags object = {}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

output loginServer string = acr.properties.loginServer
output name string = acr.name
output registryId string = acr.id
