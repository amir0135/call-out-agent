# Call-Out Agent PoC

> **"An SOP-constrained AI voice agent that places outbound calls itself, follows existing procedures exactly, logs everything, and escalates only when needed."**

## Get started in one command

Clone the repo and run the setup script. It checks prerequisites, creates a Python
virtual environment, installs all dependencies, generates a `.env` from
`.env.example`, and runs the test suite to verify the checkout:

```bash
git clone https://github.com/amir0135/call-out-agent.git
cd call-out-agent
./setup.sh
```

From there, run the stack locally with `docker compose up --build`, or deploy the
whole thing to Azure with `azd up`. See **Prerequisites** and **Quick Start** below
for the Azure details (ACS phone number, Speech resource).

## Architecture

```
Alarm System (existing)
    ↓ HTTP webhook
Call-Out Orchestrator (Azure Container Apps, Python/FastAPI)
    ↓ loads customer/store/alarm/SOP context
    ↓ initiates outbound call via ACS
ACS Call Automation
    ↓ call connected → TTS greeting
    ↓ STT recognizes callee response
    ↓ sends transcribed text to…
Foundry Hosted Agent (SOP Reasoning, Azure OpenAI GPT-4o)
    ↓ returns next SOP step + utterance
    ↓ (loop until terminal SOP state)
ACS → hang up
    ↓
Cosmos DB (audit log: transcript, SOP steps, decisions, timestamps)
Blob Storage (call recording)
App Insights (telemetry, Foundry traces)
    ↓
Next action: resolved / retry / escalate to human
```

## PoC Scope

| Included | Excluded |
|----------|----------|
| 1 customer, 1 alarm type (high temp) | Multi-customer logic |
| 1 SOP (high temperature verification) | Dispatch workflows |
| AI places and conducts outbound call | Configuration changes / setpoint writes |
| SOP-constrained conversation | Free conversation / small talk |
| Full audit trail (Cosmos DB + recordings) | Multi-language support |
| Human escalation escape hatch | Inbound call handling |
| Retry logic with thresholds | DTMF / keypad input |

## Project Structure

```
├── azure.yaml                 # azd service definitions
├── infra/                     # Bicep infrastructure (all modules)
│   ├── main.bicep
│   └── modules/               # ACS, CosmosDB, Container Apps, OpenAI, Speech, ACR, Storage, AppInsights
├── src/
│   ├── orchestrator/          # Container App — call orchestration
│   │   ├── app.py             # FastAPI: alarm intake + ACS webhook handlers
│   │   ├── call_handler.py    # ACS Call Automation SDK integration
│   │   ├── sop_engine.py      # SOP state machine (safety boundary)
│   │   ├── foundry_client.py  # HTTP client → Foundry agent (retry + circuit breaker)
│   │   ├── audit.py           # Cosmos DB audit writer
│   │   ├── retry_manager.py   # Call retry logic + escalation
│   │   └── models.py          # Pydantic domain models
│   └── agent/                 # Foundry Hosted Agent — SOP reasoning
│       ├── agent.py           # FastAPI + Azure OpenAI (GPT-4o, temp=0.1)
│       └── prompts/
│           └── sop_system.txt # System prompt with hard SOP constraints
├── sops/
│   └── high_temp_alarm.json   # Structured SOP definition
└── tests/                     # Unit + integration tests
```

## Azure Resources

| Resource | Purpose |
|----------|---------|
| Azure Communication Services | Outbound calling, STT, TTS |
| Azure OpenAI (GPT-4o) | SOP reasoning via Foundry agent |
| Azure AI Speech | Neural voice synthesis |
| Azure Container Apps | Orchestrator hosting |
| Azure Container Registry | Docker images for orchestrator + agent |
| Azure Cosmos DB (NoSQL, Serverless) | Audit events, call records |
| Azure Blob Storage | Call recordings |
| Application Insights | Telemetry + tracing |

All resources use **managed identity** (no connection strings). Entra-only auth enforced where supported.

## Prerequisites

- Azure subscription with permission to create resources and assign roles
- [Azure Developer CLI (azd)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) 1.10+
- [Azure CLI](https://docs.microsoft.com/cli/azure/install-azure-cli)
- Docker Desktop
- Python 3.11+

## Quick Start — Deploy to Azure with `azd`

The whole infrastructure (Container Apps, Cosmos, OpenAI, Speech, AI Search, ACR, App Insights, managed identities, RBAC) is defined in [infra/](infra/) and deployed by `azd up`.

### 1. Provision ACS (one-time, manual)

Azure Communication Services and phone-number provisioning are not automated in Bicep because phone-number availability is regional. Do this once in the Azure portal:

1. Create an **Azure Communication Services** resource (e.g. in `East US`).
2. Under **Phone numbers**, acquire a number with **Make calls** enabled.
3. Note the resource name, endpoint, and phone number.

### 2. Create a Speech (Cognitive Services) resource

Either create one manually in the portal (Kind = `SpeechServices`) or let the Bicep in this repo do it — the module `infra/modules/speech.bicep` creates `speech-<token>` automatically.

Grant the ACS resource's managed identity the **Cognitive Services User** role on the Speech resource — this is what lets ACS play TTS and run STT on your behalf:

```bash
ACS_MI=$(az communication show -n <acs-name> -g <rg> --query identity.principalId -o tsv)
COG_ID=$(az cognitiveservices account show -n <speech-name> -g <rg> --query id -o tsv)
az role assignment create \
  --assignee-object-id "$ACS_MI" --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services User" --scope "$COG_ID"
```

### 3. Deploy everything with `azd`

```bash
azd auth login
azd init                    # first time only
azd env set ACS_PHONE_NUMBER   "+1XXXXXXXXXX"
azd env set ACS_ENDPOINT       "https://<acs-name>.<region>.communication.azure.com"
azd env set ACS_RESOURCE_NAME  "<acs-name>"
azd up
```

`azd up` will:

- Create a resource group and provision Container Apps, Container Registry, Cosmos DB, Azure OpenAI (+ `gpt-4o` deployment), AI Search, Blob Storage, App Insights, Log Analytics.
- Build and push the `agent` and `orchestrator` Docker images to ACR.
- Deploy both Container Apps with managed identities and the RBAC assignments in `infra/modules/rbac.bicep`.
- Print the orchestrator public URL.

### 4. Post-deploy wiring

After `azd up`, set these on the orchestrator Container App (done automatically if you `azd env set` them before deploying):

- `COGNITIVE_SERVICES_ENDPOINT` — endpoint of the Speech resource from step 2.
- `ACS_CONNECTION_STRING` — **only needed if you run the orchestrator outside Container Apps** (e.g. local Docker). In Azure the managed identity handles auth.

```bash
azd env set COGNITIVE_SERVICES_ENDPOINT "https://<speech-name>.cognitiveservices.azure.com/"
azd deploy orchestrator
```

### 5. Seed the knowledge base (optional, for RAG)

```bash
pip install -r src/orchestrator/requirements.txt
python scripts/seed_knowledge.py
```

### 6. Trigger a test alarm

```bash
ORCH_URL=$(azd env get-values | grep AZURE_CONTAINER_APP_URL | cut -d= -f2 | tr -d '"')
curl -X POST "https://$ORCH_URL/api/alarm-intent" \
  -H "Content-Type: application/json" \
  -d '{
    "alarm": {
      "alarm_id": "ALM-TEST-001",
      "alarm_type": "high_temperature",
      "severity": "high",
      "store_name": "Test Store",
      "store_id": "ST-001",
      "equipment_name": "Cooler Unit A",
      "current_temp": 12.5,
      "threshold_temp": 8.0,
      "alarm_time": "2026-04-16T10:30:00Z",
      "customer_id": "CUST-001"
    },
    "phone_number": "+1XXXXXXXXXX"
  }'
```

## Integration API (for the alarm system / customer tools)

Designed for **200k alarm triggers/month**: intake is queued (Service Bus + KEDA scale-out to 50 replicas), idempotent, and results are pushed back. Full capacity analysis in [docs/scalability.md](docs/scalability.md).

| Surface | Direction | Purpose |
|---|---|---|
| `POST /api/alarm-intent` | in | Trigger a call-out. Returns `202 {intent_id}` (queued). Idempotent — webhook retries collapse. Guard with `ORCHESTRATOR_API_KEY` (`X-API-Key` header). |
| Outcome webhook | out | Terminal call result (outcome, transcript, recording URL) POSTed to `OUTCOME_WEBHOOK_URL` or per-intent `callback_url`; HMAC-signed via `OUTCOME_WEBHOOK_SECRET` (`X-Callout-Signature`). |
| `GET /api/alarm-status/{alarm_id}` | out (poll) | Attempts, outcomes, whether a retry is pending. |
| `GET /api/call-status/{call_id}` | out (poll) | Live snapshot of an in-flight call. |
| Cosmos `audit_events` / Blob recordings | out (pull) | Compliance/BI trail, 90-day TTL. |

```bash
azd env set OUTCOME_WEBHOOK_URL    "https://your-tools.example.com/callout-results"
azd env set OUTCOME_WEBHOOK_SECRET "<random-hmac-key>"
azd env set ORCHESTRATOR_API_KEY   "<random-api-key>"
```

## Run locally (Docker Compose)

For development and iteration without re-deploying to Azure.

### 1. Prepare `.env`

```bash
cp .env.example .env
# Fill in the values — minimum required:
#   ACS_ENDPOINT, ACS_PHONE_NUMBER, ACS_CONNECTION_STRING,
#   COGNITIVE_SERVICES_ENDPOINT, CALLBACK_BASE_URL,
#   AZURE_OPENAI_ENDPOINT, AZURE_AI_SEARCH_ENDPOINT
```

Fetch the ACS connection string:

```bash
az communication list-key -n <acs-name> -g <rg> \
  --query primaryConnectionString -o tsv
```

### 2. Expose port 8000 publicly so ACS can call back

ACS needs a public HTTPS URL to send call events to. Locally, use [Microsoft devtunnels](https://learn.microsoft.com/azure/developer/dev-tunnels/):

```bash
devtunnel user login
devtunnel host --port-numbers 8000 --allow-anonymous
```

Copy the `https://...devtunnels.ms` URL into `.env` as `CALLBACK_BASE_URL`.

### 3. Start the stack

```bash
docker compose up --build
```

Health checks:

```bash
curl http://localhost:8000/health
curl "$CALLBACK_BASE_URL/health"
```

### 4. Trigger a test call

```bash
./scripts/test_call.sh +YOURPHONE "$CALLBACK_BASE_URL"
```

## Run tests

```bash
pip install -r src/orchestrator/requirements.txt
pip install pytest pytest-asyncio pytest-httpx
pytest
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `HTTP 502 — DefaultAzureCredential failed` when triggering a call | Orchestrator container has no AAD creds (local Docker) | Set `ACS_CONNECTION_STRING` in `.env` and restart: `docker compose up -d --force-recreate orchestrator` |
| Call connects but callee hears silence; logs show ACS error `8522` | Cognitive Services endpoint not linked at call setup | Set `COGNITIVE_SERVICES_ENDPOINT` and confirm ACS MI has `Cognitive Services User` on the Speech resource |
| Call never reaches orchestrator (no `/api/acs-callback` entries in logs) | `CALLBACK_BASE_URL` is stale (tunnel died or changed) | Restart the tunnel, update `.env`, re-deploy: `docker compose up -d --force-recreate orchestrator` |
| Cosmos Data Explorer shows no data | Your Azure user lacks the Cosmos data-plane role | `az cosmosdb sql role assignment create --account-name <cosmos> --resource-group <rg> --scope "/" --principal-id $(az ad signed-in-user show --query id -o tsv) --role-definition-id 00000000-0000-0000-0000-000000000002` |

## Design Principles

- **AI is SOP-constrained** — the SOP engine is the safety boundary, not the LLM
- **Fail safe** — unclear responses escalate to humans, never guess
- **Full audit trail** — every event logged to Cosmos DB, calls recorded
- **Managed identity everywhere** — no secrets in config
- **Minimal PoC scope** — one customer, one alarm, one SOP

## What the PoC Proves

1. AI can **reliably place outbound calls**
2. AI can **follow SOPs exactly** without deviation
3. AI can **reduce human call load** for routine alarm verification
4. **No critical alarms are missed** — escalation guarantees human backup
5. **Everything is auditable** — full compliance trail

## Important Notes

- **ACS Phone Number**: Must be provisioned manually in the Azure portal (not automated in Bicep due to regional availability constraints)
- **Call Recording Consent**: The SOP greeting includes a recording consent notice. Legal should approve the exact wording before production use
- **Turn Latency**: Target < 2s for STT → Agent → TTS round-trip. Consider pre-generating TTS for known SOP steps if latency is unacceptable
