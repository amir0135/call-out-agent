# Call-Out Agent — Technical Demo Walkthrough

> An SOP-constrained AI voice agent that places outbound calls itself, follows an existing
> procedure exactly, logs every turn, and escalates to a human only when it has to.

Audience: engineering. This is the script + reference for a ~10-minute live technical demo.
For the presenter cheat-sheet (talking track, stage directions) see
[Call-Out-Agent-Demo-Script.docx](Call-Out-Agent-Demo-Script.docx).

---

## 0. One-paragraph pitch

When an alarm fires at a site (e.g. a refrigeration unit above its temperature threshold), a
human operator normally calls the site, reads a checklist, waits for an answer, and decides what
to do. It is repetitive, 24/7, and most calls end with "yep, we're on it." This agent makes that
call itself over the phone, follows the **same** checklist, and only escalates when it genuinely
needs a human. The design point is **SOP-constrained**: the LLM does not improvise — it may only
pick the next step allowed by a Standard Operating Procedure. AI speed with checklist safety.

---

## 1. Live demo runbook (fast path)

Everything is wrapped in one idempotent script.

```bash
cd call-out-agent
./scripts/demo.sh            # frees :8000, cleans stale containers, (re)hosts a devtunnel,
                             # writes CALLBACK_BASE_URL into .env, builds+starts both
                             # containers, waits for /health locally and via the tunnel
```

When it prints **"Demo is ready"**, open three terminals:

```bash
# Terminal A — the demo.sh summary (leave visible)

# Terminal B — audit stream
docker compose logs -f orchestrator | grep --line-buffered -E 'AUDIT|recognized|utterance|escalat'

# Terminal C — trigger (do not press Enter until you're narrating)
export TUNNEL_URL="$(cat /tmp/callout-tunnel-url)"
./scripts/test_call.sh +YOURPHONE "$TUNNEL_URL"
```

Health / lifecycle:

```bash
./scripts/demo.sh status     # containers + local/tunnel health + tunnel URL
./scripts/demo.sh tunnel     # re-host tunnel only, sync .env
./scripts/demo.sh stop       # tear down containers + tunnel
```

> Prereqs: Docker Desktop, devtunnel CLI, and a populated `.env` (ACS endpoint + number +
> connection string, Cognitive Services endpoint, OpenAI + AI Search endpoints). Placing a call
> dials a real PSTN number and costs real telephony minutes.

---

## 2. What to show — the beats

| Beat | Open this | Point being made |
|---|---|---|
| 1. The contract | `sops/high_temp_alarm.json` | The SOP is structured JSON: states, prompts, accepted keywords, next-state. A human authored it once; the agent never deviates. |
| 2. The guardrail | `src/agent/prompts/sop_system.txt` | System prompt, temp 0.1. The model may only return one of the SOP's allowed next steps. If it invents one, the orchestrator refuses it. |
| 3. Architecture | `README.md` / the arch GIFs | Two services: deterministic orchestrator + reasoning agent. ACS handles the phone leg, STT and TTS. |
| 4. Trigger | Terminal C | Post the *same* alarm-intent payload the real alarm system would send. Nothing upstream changed. |
| 5. Audit stream | Terminal B | Every turn is an audit event — call initiated, connected, SOP step, recognized text, agent response. Same shape in dev (stdout) and prod (Cosmos). |
| 6. The call | The phone (speaker) | Play the site manager. Happy path resolves; two unclear answers → escalation. |
| 7. Timeline | Terminal B after hangup | Full replayable timeline: utterances, transcripts, decisions, SOP states, timestamps. |

Escalation variant: on the second question, answer with silence/gibberish. After two unclear
responses the agent announces it is connecting a human, then stops. "Jailbreak at worst becomes
escalation."

---

## 3. Architecture — why each box exists

```
Alarm system ──HTTP──▶ Orchestrator (Azure Container Apps, FastAPI)
                          │  loads customer/site/alarm/SOP context
                          │  owns the call lifecycle + SOP state machine   ← deterministic, no AI
                          ▼
                       ACS Call Automation ──▶ PSTN call, STT, TTS
                          │
                          ▼
                       Reasoning agent (Foundry hosted, Azure OpenAI) ← language only, temp 0.1
                          │  returns the next SOP step + utterance
                          ▼
   Cosmos DB (audit, partitioned by callId) · Blob (recordings) · App Insights (per-turn latency)
   Azure AI Search (customer/equipment grounding) · Managed identity + RBAC everywhere
```

Key decisions:

- **Split orchestrator from agent.** The orchestrator owns a testable state machine; the agent
  only does language understanding. If the LLM misbehaves, the orchestrator rejects the step.
  The SOP is the contract; the LLM is a suggestion engine.
- **ACS for telephony**, not Twilio/SIP: native outbound PSTN, Call Automation SDK, STT/TTS —
  one identity model, one bill, one support channel.
- **Cosmos serverless, append-only**, partitioned by `callId` → reconstructing a call is a
  single-partition query; cost tracks alarms, not uptime.
- **Managed identity + RBAC only.** No connection strings in config; Cosmos local auth disabled.
- **Container Apps** scales out on concurrent alarms (KEDA), scales toward zero when idle.

---

## 4. The safety model (the part engineers ask about)

The SOP engine — not the LLM — is the safety boundary:

1. Each SOP state declares its prompt, the keyword branches it accepts, and the next state.
2. The agent proposes a next step; the orchestrator validates it against the current state's
   allowed transitions.
3. Anything unmatched falls to an `unclear` branch and re-asks the **same** question once.
4. After two unclear responses → escalate to a human. No guessing, ever.

So the failure modes are bounded: a confused caller, a jailbreak attempt, or a model that returns
garbage all converge on the same safe outcome — escalation — and all of it is auditable.

---

## 5. Voice engines & latency

Voice-to-voice latency (true end of caller speech → first agent audio) target: **< 800 ms**,
ideal 500–700 ms. Three engines are switchable at runtime via the `STREAMING_ENGINE` env var:

| Engine | How it works | Typical first-audio | Notes |
|---|---|---|---|
| `chained` | ACS bidi media stream → Azure Speech STT → SOP/agent → streaming TTS | **~400 ms** | Fastest; deterministic; only engine with exact SOP step tracking. Demo default. |
| `realtime` | GPT speech-to-speech (Azure OpenAI Realtime) | ~600–800 ms | Most natural turn-taking; native barge-in. |
| `voicelive` | Managed Voice Live (same brain + Azure neural voices, semantic VAD) | ~650–940 ms | Best voice quality; per-language native voices for future multi-language. |

Why chained is fast enough: the win was moving off ACS `recognize` (1 s end-silence floor) to ACS
**bidirectional media streaming** into Azure Speech continuous recognition, where the
segmentation-silence timeout can be ~150–300 ms, with TTS started on the first clause and audio
co-located with ACS in-region. Per-turn latency and % within target are exposed at
`/metrics/latency`.

---

## 6. Scale story (PoC → pilot → production)

The demo is intentionally narrow: one customer, one alarm type, one SOP — because the hard part is
proving the guardrails hold, not the happy path.

- **Intake is queued**: `POST /api/alarm-intent` → Service Bus, idempotent (webhook retries
  collapse), KEDA scales the orchestrator out (1 → ~50 replicas) to drain bursts.
- **Results are pushed back**: terminal outcome (resolved / retry / escalate / failed) + transcript
  + recording URL POSTed to an HMAC-signed outcome webhook, plus poll endpoints
  (`/api/alarm-status/{id}`, `/api/call-status/{id}`).
- **State is externalized**: pluggable session/retry stores (in-memory for dev, Cosmos for scale)
  so any replica can operate any call.
- **Cost shape** (illustrative, model-based): pilot ≈ low-hundreds $/mo (fixed infra dominated);
  at 200k alarms/mo the bill is ~85–90% usage — PSTN minutes + Speech STT/TTS — not platform infra.
  The biggest lever is the telephony path (direct routing vs retail per-minute) and call duration.

Full capacity analysis: [scalability.md](scalability.md). Latency deep-dive:
[latency-tuning.md](latency-tuning.md).

---

## 7. Q&A cheat sheet

- **Why not let the model free-talk?** Liability, auditability, regulation. The SOP is a contract;
  the model doesn't renegotiate it mid-call, and we can show exactly which step ran and why.
- **What if the agent is down?** Circuit breaker in the Foundry client → fall back to escalation.
  A safety check never silently fails.
- **Latency?** Sub-2 s per turn end to end (STT + agent + TTS), measured per turn in App Insights;
  fixed SOP prompts can be pre-rendered so only dynamic utterances hit the model at call time.
- **Consent/recording?** The greeting states the call may be recorded; recordings live in Blob with
  retention + lifecycle policies; production adds a per-region consent matrix.
- **Cost per call?** A 60–120 s call is a few cents of tokens + ACS minutes — the case is freeing
  humans for the calls that actually need a human.

---

## 8. If something breaks mid-demo

```bash
./scripts/demo.sh stop && ./scripts/demo.sh      # full restart (ports, containers, tunnel)
./scripts/demo.sh status                          # both /health must be {"status":"ok"}
docker compose logs --tail 80 orchestrator        # phone doesn't ring / 502 / silence
```

- **HTTP 502 "DefaultAzureCredential failed"** → `ACS_CONNECTION_STRING` missing in `.env`
  (local container has no AAD creds). Set it, `docker compose up -d --force-recreate orchestrator`.
- **Call connects but silence (ACS 8522)** → `COGNITIVE_SERVICES_ENDPOINT` not set / ACS MI lacks
  *Cognitive Services User* on the Speech resource.
- **ACS callback 404s** → stale tunnel: `./scripts/demo.sh tunnel` then force-recreate orchestrator.
- **Model says something odd** → teaching moment: the SOP engine refuses invalid steps and retries.
