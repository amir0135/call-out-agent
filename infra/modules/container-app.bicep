// ---------------------------------------------------------------------------
// Azure Container App
// ---------------------------------------------------------------------------

param location string
param managedEnvironmentId string
param appName string
param containerName string = 'app'
param acrLoginServer string
param containerImage string = ''
param targetPort int = 8000
param tags object = {}
param envVars array = []

// --- Optional Service Bus KEDA scaler ---------------------------------------
// When set, adds a queue-depth-based scale rule using the container app's
// own managed identity. Leave empty for app containers that don't consume
// from a queue (e.g. the agent).
@description('Service Bus namespace (without .servicebus.windows.net). Empty = no queue scaler.')
param serviceBusNamespace string = ''
@description('Service Bus queue name. Required if serviceBusNamespace is set.')
param serviceBusQueueName string = ''
@description('Target messages per replica. KEDA adds a replica per N messages backlog.')
param serviceBusMessagesPerReplica int = 20

@description('Max replicas. Keep at 1 when there is no shared session store (in-memory state is per-replica).')
param maxReplicas int = 20

@description('vCPU cores (valid ACA combo with memory, e.g. 0.5/1Gi, 1.0/2Gi, 2.0/4Gi).')
param cpuCores string = '0.5'

@description('Memory size (must pair with cpuCores per ACA rules).')
param memorySize string = '1Gi'

var queueScaler = empty(serviceBusNamespace) ? [] : [
  {
    name: 'queue-depth-scaling'
    custom: {
      type: 'azure-servicebus'
      metadata: {
        namespace: serviceBusNamespace
        queueName: serviceBusQueueName
        messageCount: string(serviceBusMessagesPerReplica)
      }
      identity: 'system'
    }
  }
]

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: managedEnvironmentId
    // Required for workload-profiles environments; ignored value on legacy envs
    // is harmless because our env always defines a Consumption profile.
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: true
        targetPort: targetPort
        transport: 'http'
      }
      registries: !empty(acrLoginServer) ? [
        {
          server: acrLoginServer
          identity: 'system'
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: containerName
          image: !empty(containerImage) ? containerImage : 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: {
            cpu: json(cpuCores)
            memory: memorySize
          }
          env: envVars
        }
      ]
      scale: {
        minReplicas: 1
        // Raised from 3 → 20 to support ~50k alarms/month with bursty load.
        // Requires shared-state stores (Redis sessions, Cosmos retry state)
        // so any replica can serve any ACS webhook.
        maxReplicas: maxReplicas
        rules: union([
          {
            name: 'http-scaling'
            http: {
              metadata: {
                concurrentRequests: '10'
              }
            }
          }
        ], queueScaler)
      }
    }
  }
}

output fqdn string = containerApp.properties.configuration.ingress.fqdn
output principalId string = containerApp.identity.principalId
output appName string = containerApp.name
