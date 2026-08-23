// ---------------------------------------------------------------------------
// Azure AI Speech — STT / TTS for voice calls
// ---------------------------------------------------------------------------

param location string
param name string
param tags object = {}

resource speechAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: name
  location: location
  tags: tags
  kind: 'SpeechServices'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: name
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

output endpoint string = speechAccount.properties.endpoint
output accountId string = speechAccount.id
output region string = location
