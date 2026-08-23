// ---------------------------------------------------------------------------
// Azure Communication Services
// ---------------------------------------------------------------------------

param location string
param name string
param tags object = {}

resource acs 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    dataLocation: 'United States'
  }
}

output endpoint string = 'https://${acs.name}.communication.azure.com'
output resourceId string = acs.id
