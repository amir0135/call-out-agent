// ---------------------------------------------------------------------------
// Azure Cache for Redis — shared session store for the orchestrator
//
// The orchestrator must run in >1 replica to serve ~50k alarms/month, so the
// per-call session state (conversation history, current SOP step) is moved
// out of process memory and into Redis. Any replica can then look up a
// session keyed by the ACS call connection id.
//
// PoC sizing: Standard C1 (1 GB, ~6,000 ops/sec). Bump to C2 / Premium for
// production high-availability and zone redundancy.
// ---------------------------------------------------------------------------

param location string
param name string
param tags object = {}

@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param skuName string = 'Standard'

@allowed([
  0
  1
  2
  3
  4
  5
  6
])
param skuCapacity int = 1

resource redis 'Microsoft.Cache/redis@2024-03-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      name: skuName
      family: skuName == 'Premium' ? 'P' : 'C'
      capacity: skuCapacity
    }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'  // Lock down behind VNet in production
    redisConfiguration: {
      // Evict the least-recently-used key when full — session TTL still
      // gives us a hard upper bound, this just protects against runaway
      // memory pressure.
      'maxmemory-policy': 'allkeys-lru'
    }
  }
}

// The orchestrator authenticates with the primary access key, retrieved via
// listKeys at deployment time. For production, switch to Microsoft Entra
// authentication (preview on Azure Cache for Redis) and remove the key.
#disable-next-line outputs-should-not-contain-secrets
output primaryKey string = redis.listKeys().primaryKey
output hostName string = redis.properties.hostName
output sslPort int = redis.properties.sslPort
output resourceId string = redis.id
output name string = redis.name
