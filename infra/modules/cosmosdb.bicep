// ---------------------------------------------------------------------------
// Azure Cosmos DB (NoSQL) — audit events, call records, SOP executions
// ---------------------------------------------------------------------------

param location string
param name string
param tags object = {}

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: name
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    disableLocalAuth: true  // Enforce Entra-only auth
    capabilities: [
      {
        name: 'EnableServerless'  // Serverless for PoC cost efficiency
      }
    ]
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmosAccount
  name: 'callout'
  properties: {
    resource: {
      id: 'callout'
    }
  }
}

resource callsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'calls'
  properties: {
    resource: {
      id: 'calls'
      partitionKey: {
        paths: ['/callId']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        includedPaths: [{ path: '/*' }]
        excludedPaths: [{ path: '/"_etag"/?' }]
      }
    }
  }
}

resource auditEventsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'audit_events'
  properties: {
    resource: {
      id: 'audit_events'
      partitionKey: {
        paths: ['/callId']
        kind: 'Hash'
      }
      defaultTtl: 7776000  // 90 days retention
      indexingPolicy: {
        indexingMode: 'consistent'
        includedPaths: [{ path: '/*' }]
        excludedPaths: [{ path: '/"_etag"/?' }]
      }
    }
  }
}

resource sopExecutionsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'sop_executions'
  properties: {
    resource: {
      id: 'sop_executions'
      partitionKey: {
        paths: ['/callId']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        includedPaths: [{ path: '/*' }]
        excludedPaths: [{ path: '/"_etag"/?' }]
      }
    }
  }
}

// Retry state — keyed by alarmId so retry counters survive replica restarts
// and stay coherent across multiple orchestrator replicas.
resource retryStateContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'retry_state'
  properties: {
    resource: {
      id: 'retry_state'
      partitionKey: {
        paths: ['/alarmId']
        kind: 'Hash'
      }
      defaultTtl: 2592000  // 30 days — retry records expire after an alarm is dormant
      indexingPolicy: {
        indexingMode: 'consistent'
        includedPaths: [{ path: '/*' }]
        excludedPaths: [{ path: '/"_etag"/?' }]
      }
    }
  }
}

// Active call sessions — shared session store for multi-replica scale-out in
// regions where Azure Cache for Redis is unavailable (classic SKU retired).
// Partitioned by /id (== call_id); TTL reclaims orphaned sessions.
resource callSessionsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'call_sessions'
  properties: {
    resource: {
      id: 'call_sessions'
      partitionKey: {
        paths: ['/id']
        kind: 'Hash'
      }
      defaultTtl: 3600  // 1 hour — well beyond the longest plausible call
      indexingPolicy: {
        indexingMode: 'consistent'
        includedPaths: [{ path: '/*' }]
        excludedPaths: [{ path: '/"_etag"/?' }]
      }
    }
  }
}

output endpoint string = cosmosAccount.properties.documentEndpoint
output accountName string = cosmosAccount.name
output accountId string = cosmosAccount.id
