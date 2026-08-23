# Scalability & Integration Assessment — Call-Out Agent

**Audience:** Syed's team
**Author:** Amira (with Henrik)
**Updated:** 8 July 2026
**Target load:** **200 000 alarm triggers / month** (≈ 4.6 calls/min steady, peak-hour ~15–25/min, burst events of thousands after a regional power blip)

---

## TL;DR

| Layer | Scales to 200k/mo? | Notes |
|---|---|---|
| Alarm intake (`POST /api/alarm-intent`) | ✅ | Service Bus queue always deployed; endpoint enqueues + returns 202. Idempotent via deterministic `intent_id` + broker message dedup. |
| Orchestrator (FastAPI on Container Apps) | ✅ 1 → 50 replicas | HTTP-scaled + KEDA `azure-servicebus` queue-depth scaler. |
| Session store | ✅ | Redis when deployed, **Cosmos `call_sessions` container otherwise** — horizontal scale no longer depends on Redis availability. |
| Retry/de-dup store | ✅ | Cosmos `retry_state`, ETag optimistic concurrency. |
| Audit log (Cosmos Serverless) | ✅ | ~10 events/call → ~2 M events/mo, well within Serverless (5 000 RU/s ≈ 300+ writes/s). Switch to autoscale provisioned if RU throttling appears. |
| Outbound integration | ✅ | Outcome webhook (HMAC-signed) + status polling endpoints — see [§5](#5-integration-surface). |
| Azure Communication Services | ⚠️ **quota ticket required** | Default ~100 concurrent outbound calls/resource. 200k/mo needs uplift for burst drain — see [§3.1](#31-acs-concurrent-call-quota). |
| gpt-realtime (when `STREAMING_ENGINE=realtime`) | ⚠️ **quota is the concurrency ceiling** | Deployed at cap 8 (region quota 10). See [§3.2](#32-gpt-realtime-capacity). |

---

## 1. The 200k/month envelope

Assumptions: avg call 90 s (incl. ring time), ~10–15 webhook/WS events per call.

| Metric | Value |
|---|---|
| Steady rate | 200 000 / (30·24·60) ≈ **4.6 calls/min** |
| Concurrent calls, steady | 4.6/min × 1.5 min ≈ **7 in flight** |
| Peak-hour rate (alarms cluster in business hours, ~4×) | **~20 calls/min → ~30 concurrent** |
| Burst event (regional outage, 5 000 alarms at once) | Queue absorbs all; drain rate bounded by ACS concurrency (see below) |
| Burst drain at 100 concurrent / 90 s calls | ~66 calls/min → 5 000 alarms in **~75 min** |
| Burst drain at 500 concurrent (post quota uplift) | ~330 calls/min → 5 000 alarms in **~15 min** |

**Conclusion:** steady-state 200k/mo is easy; the engineering problem is **burst drain rate**, and that is bounded by ACS concurrent-call quota + realtime-model capacity, not by this codebase.

---

## 2. Architecture at 200k/month

```
Alarm system ──POST /api/alarm-intent (X-API-Key)──► 202 {intent_id}
                    │
                    ▼
             Service Bus queue (alarm-intake, msg-dedup on intent_id)
                    │  KEDA azure-servicebus scaler (20 msgs/replica)
                    ▼
     Container Apps orchestrator — min 1, max 50 replicas
        │  sessions: Redis ▸ or Cosmos call_sessions (TTL 1h)
        │  retry state: Cosmos retry_state (ETag concurrency)
        │  audit: Cosmos audit_events (TTL 90d)
        ▼
     ACS Call Automation (webhooks/media-WS land on ANY replica —
     all state is shared; at-least-once events deduped per session)
                    │
                    ▼  terminal outcome
     ┌─ Outcome webhook → customer tools (HMAC-signed POST)
     └─ GET /api/alarm-status/{alarm_id} (poll)
```

Key properties:

- **Every replica is interchangeable.** Sessions, retry state, and processed-event IDs live in shared stores; ACS webhooks and media-stream WebSockets can land anywhere.
- **Ingestion is decoupled from dialing.** A 5 000-alarm burst returns 202 in milliseconds each; the queue meters dialing at whatever ACS can sustain.
- **At-least-once everywhere is safe.** Deterministic `intent_id` (uuid5 of alarm identity), Service Bus message dedup, per-session ACS event-ID dedup, and the retry store's `should_attempt_call` gate all collapse duplicates.

## 3. External quotas — the real ceilings

### 3.1 ACS concurrent-call quota
Default ~100 concurrent outbound calls per ACS resource. Required actions before go-live:

1. **Open the quota ticket now** (3–5 business-day lead time). Ask for ≥ 500 concurrent for a 15-minute burst-drain SLA on 5 000-alarm events.
2. Load-test in a non-prod resource; watch for `(8523) Call rate limit exceeded`.
3. Fallback/scale-beyond option: shard across multiple ACS resources — `region_router.py` + per-region client cache in `call_handler.py` already support N resources; add entries to `sops/regions.json`.

### 3.2 gpt-realtime capacity
`STREAMING_ENGINE=realtime` gives each in-flight call a live GPT speech-to-speech session. Current deployment: GlobalStandard capacity 8 (regional quota 10). At ~30 peak concurrent calls this throttles.

Options, in order of preference:
1. Request gpt-realtime quota uplift (target ≥ peak concurrent calls).
2. Spill overflow to the **chained engine** (`STREAMING_ENGINE=chained`, Speech STT→SOP→TTS) — deterministic, no realtime-model dependency, ~450 ms v2v measured.
3. Multi-region OpenAI resources behind the region router.

### 3.3 Azure OpenAI chat quota (chained engine / agent)
gpt-5-mini at 30k+ TPM covers ~1 call/s of SOP reasoning comfortably; bump via quota request if burst drain exceeds that.

## 4. What changed to support 200k/month (July 2026)

1. **Cosmos-backed session store** (`session_store.py::CosmosSessionStore`, `call_sessions` container, TTL 1 h). Horizontal scale no longer depends on Azure Cache for Redis — which is retired/unavailable in some regions (e.g. Sweden Central). Factory precedence: `REDIS_URL` ▸ `SESSION_COSMOS_CONTAINER` ▸ in-memory (single replica, dev only).
2. **Service Bus intake decoupled from Redis.** Previously `deployRedis=false` also disabled the queue and pinned the app to 1 replica. Now the queue + KEDA scaler + multi-replica scale are always on.
3. **`maxReplicas` 20 → 50** (`ORCHESTRATOR_MAX_REPLICAS` azd env var). 50 replicas × 2 vCPU handles ~500 concurrent streaming calls of webhook/WS load.
4. **Outbound integration surface** — outcome webhook + status APIs (below).
5. **`aiohttp` added to requirements** — the async Service Bus consumer needs it (`azure.identity.aio`); previously crashed at startup when the queue was enabled.

## 5. Integration surface

Everything the customer's tools need to plug the agent into alarm pipelines, ticketing, and monitoring:

### Inbound (their alarm system → us)
- `POST /api/alarm-intent` — JSON `CallIntent`; guarded by `X-API-Key` (`ORCHESTRATOR_API_KEY`). Returns **202 `{status: queued, intent_id}`** when the queue is on. Retries of the same alarm payload are idempotent (deterministic `intent_id` → broker dedup → retry-store gate).
- Optional per-intent `callback_url` — route this alarm's result to a specific system.

### Outbound (us → their tools)
- **Outcome webhook**: on every terminal call state we POST a JSON summary (intent/alarm/call IDs, outcome, attempt, timestamps, final SOP step, recording URL, full transcript) to `OUTCOME_WEBHOOK_URL` or the per-intent `callback_url`. Signed with `X-Callout-Signature` (hex HMAC-SHA256 of the body, key `OUTCOME_WEBHOOK_SECRET`) — receivers must verify. Bounded retries (1 s/5 s/30 s); on final failure the audit trail remains the source of truth.
- `GET /api/alarm-status/{alarm_id}` — poll-based twin: attempts, outcomes, `will_retry`.
- `GET /api/call-status/{call_id}` — live snapshot of an in-flight call.
- **Cosmos audit trail** — `audit_events` (90-day TTL) for compliance/BI pulls; recordings in Blob.
- `GET /metrics/latency` + App Insights for monitoring integration.

## 6. Validation checklist for go-live

1. **Startup logs** must show `CosmosSessionStore initialized` (or Redis) and `CosmosRetryStore initialized` — never the `SAFE ONLY WITH A SINGLE REPLICA` warnings.
2. **ACS quota ticket** filed and confirmed ≥ target burst concurrency ([§3.1](#31-acs-concurrent-call-quota)).
3. **gpt-realtime quota** ≥ peak concurrent calls, or chained-engine fallback agreed ([§3.2](#32-gpt-realtime-capacity)).
4. **Burst load test**: enqueue 1 000 synthetic intents (`scripts/simulate_call.py`), verify 202s in < 1 s each, `az containerapp replica list` shows scale-out, queue drains, zero duplicate calls.
5. **Webhook receiver** implemented + signature verification tested (`tests/test_outcome_notifier.py` documents the contract).
6. **Cosmos RU monitoring**: alert on 429s; switch `EnableServerless` → autoscale provisioned throughput if sustained.
