// ---------------------------------------------------------------------------
// Azure Container Apps Environment
// ---------------------------------------------------------------------------

param location string
param name string
param logAnalyticsId string = ''
param tags object = {}

resource containerAppEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
    // Workload-profiles environment (Consumption profile) — uses a newer
    // capacity pool than legacy consumption-only envs, which helps in regions
    // reporting AKSCapacityHeavyUsage for the legacy pool.
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

output environmentId string = containerAppEnvironment.id
output name string = containerAppEnvironment.name
output defaultDomain string = containerAppEnvironment.properties.defaultDomain
