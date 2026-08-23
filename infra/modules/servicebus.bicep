// ---------------------------------------------------------------------------
// Service Bus namespace + alarm-intake queue
// ---------------------------------------------------------------------------
// Sits in front of the orchestrator to absorb bursty alarm volumes
// (e.g. regional power outage triggers thousands of high-temp alarms at
// once). POST /api/alarm-intent enqueues to this queue and returns 202
// immediately; KEDA on Container Apps scales orchestrator replicas
// based on queue depth so consumers fan out horizontally.

@description('Location for the Service Bus namespace.')
param location string

@description('Globally unique Service Bus namespace name.')
param name string

@description('Tags applied to all resources.')
param tags object = {}

@description('Queue name used by the orchestrator.')
param queueName string = 'alarm-intake'

@description('SKU. Standard supports topics and is the cheapest with the features we need.')
@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param skuName string = 'Standard'

resource serviceBus 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuName
  }
  properties: {
    disableLocalAuth: true // Entra-only auth — managed identity from orchestrator
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

resource queue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: serviceBus
  name: queueName
  properties: {
    // Duplicate detection by alarm_id (set as message_id by the producer).
    // Protects against double-sends from a flaky alarm system retry loop
    // within the 10-minute window.
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'PT10M'
    // Lock long enough to complete a typical 60-90s outbound call.
    lockDuration: 'PT5M'
    // Auto-delete on idle: never (keep the queue around).
    maxSizeInMegabytes: 1024
    // Dead-letter after 5 failed delivery attempts.
    maxDeliveryCount: 5
    // Retain queued messages for 1 day (alarms older than that aren't useful).
    defaultMessageTimeToLive: 'P1D'
    deadLetteringOnMessageExpiration: true
  }
}

output namespaceName string = serviceBus.name
// Fully qualified namespace, e.g. 'sb-abc123.servicebus.windows.net' — passed
// to the orchestrator as SERVICEBUS_NAMESPACE (without the suffix; the SDK
// appends '.servicebus.windows.net' itself).
output namespace string = serviceBus.name
output queueName string = queue.name
output namespaceId string = serviceBus.id
