"""Call-Out Agent Orchestrator — FastAPI application.

Receives alarm intents, places outbound calls via ACS, drives the SOP
conversation loop through the Foundry hosted agent, and logs everything.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .audit import AuditLogger
from .call_handler import CallHandler
from .foundry_client import FoundryClient
from .outcome_notifier import OutcomeNotifier
from .queue import AlarmIntentConsumer, create_queue_client
from .region_router import create_region_router
from .session_store import create_session_store
from .retry_store import create_retry_store
from .sop_catalog import SOPCatalog
from .tenant_registry import TenantRegistry
from .models import (
    AgentRequest,
    CallIntent,
    CallOutcome,
    CallSession,
    SOPStepType,
)
from .retry_manager import RetryManager
from .sop_engine import SOPEngine
from .latency import LatencyTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _extract_recording_url(data: dict) -> str | None:
    """Extract a downloadable recording URL from ACS recording callback payloads."""
    direct_candidates = (
        data.get("recordingUrl"),
        data.get("recordingLocation"),
        data.get("contentLocation"),
    )
    for candidate in direct_candidates:
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate

    storage_info = data.get("recordingStorageInfo", {}) or {}
    chunks = storage_info.get("recordingChunks", []) or data.get("recordingChunks", []) or []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        candidate = chunk.get("contentLocation") or chunk.get("metadataLocation")
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
    return None

# ---------------------------------------------------------------------------
# Configuration (from environment)
# ---------------------------------------------------------------------------
ACS_ENDPOINT = os.environ.get("ACS_ENDPOINT", "")
ACS_PHONE_NUMBER = os.environ.get("ACS_PHONE_NUMBER", "")
ACS_CONNECTION_STRING = os.environ.get("ACS_CONNECTION_STRING", "")
CALLBACK_BASE_URL = os.environ.get("CALLBACK_BASE_URL", "")
COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT", "")
FOUNDRY_AGENT_ENDPOINT = os.environ.get("FOUNDRY_AGENT_ENDPOINT", "")
SPEECH_REGION = os.environ.get("SPEECH_REGION", "eastus")
SPEECH_VOICE = os.environ.get("SPEECH_VOICE", "en-US-JennyNeural")
ACS_SPEECH_LANGUAGE = os.environ.get("ACS_SPEECH_LANGUAGE", "en-US")
COGNITIVE_SERVICES_ENDPOINT = os.environ.get("COGNITIVE_SERVICES_ENDPOINT", "")

# --- API-key guard (opt-in) --------------------------------------------
# When ORCHESTRATOR_API_KEY is set, callers of POST /api/alarm-intent must
# present a matching X-API-Key header. Leave unset to keep the endpoint
# open (unchanged behaviour for the alarm-system webhook, tests, and local
# dev). Set it before exposing the endpoint to M365 Copilot / Teams so a
# chat user cannot place real phone calls anonymously.
ORCHESTRATOR_API_KEY = os.environ.get("ORCHESTRATOR_API_KEY", "")


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency: enforce X-API-Key when a key is configured."""
    if ORCHESTRATOR_API_KEY and x_api_key != ORCHESTRATOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

# --- ACS recognize tuning (fast reaction, calm voice) -------------------
# Two independent things: how fast SHE talks (SPEECH_RATE, kept calm) and
# how quickly she REACTS after the caller stops (these timeouts). We want
# the reaction snappy, so keep end-silence near the ACS floor (~1s; the
# SDK rounds sub-second up to 1s anyway). Barge-in stays OFF so a quick
# reaction never turns into talking over the caller.
ACS_END_SILENCE_TIMEOUT = float(os.environ.get("ACS_END_SILENCE_TIMEOUT", "1.2"))
ACS_END_SILENCE_TIMEOUT_FAST = float(os.environ.get("ACS_END_SILENCE_TIMEOUT_FAST", "1.0"))
ACS_INITIAL_SILENCE_TIMEOUT = float(os.environ.get("ACS_INITIAL_SILENCE_TIMEOUT", "5.0"))
# Barge-in OFF by default: with it on, partial recognitions during playback
# make the agent talk over the caller. Keep false until a streaming pipeline
# can reliably distinguish speech from line noise.
ACS_INTERRUPT_PROMPT = os.environ.get("ACS_INTERRUPT_PROMPT", "false").lower() in ("1", "true", "yes")
# First-turn barge-in also off for the same reason.
ACS_FIRST_TURN_BARGE_IN = os.environ.get("ACS_FIRST_TURN_BARGE_IN", "false").lower() in (
    "1",
    "true",
    "yes",
)
# DTMF fallback so the caller always has a path forward when STT misfires.
ACS_ENABLE_DTMF_FALLBACK = os.environ.get("ACS_ENABLE_DTMF_FALLBACK", "true").lower() in ("1", "true", "yes")
# SSML prosody rate applied to every TTS playback. "-5%" gives a calmer,
# more human cadence; set to "" for the voice's default speed.
SPEECH_RATE = os.environ.get("SPEECH_RATE", "-5%")

# --- Shared-state stores (multi-replica safety) -------------------------
# REDIS_URL
#   Set to enable RedisSessionStore so any orchestrator replica can
#   serve any ACS webhook. Without it, sessions live in process memory
#   and the deployment must be pinned to a single replica.
# RETRY_STATE_COSMOS_CONTAINER
#   Set to enable CosmosRetryStore (uses the existing COSMOS_ENDPOINT).
#   Without it, retry state lives in process memory and is lost on
#   restart.
REDIS_URL = os.environ.get("REDIS_URL", "")
RETRY_STATE_COSMOS_CONTAINER = os.environ.get("RETRY_STATE_COSMOS_CONTAINER", "")

# --- Service Bus (burst-resilient alarm intake) -------------------------
# When SERVICEBUS_NAMESPACE is set, POST /api/alarm-intent enqueues and
# returns 202 immediately; a background consumer drains the queue and
# runs the existing processing pipeline. KEDA on Container Apps scales
# replicas based on queue depth. When unset, behaviour is unchanged
# (synchronous processing) — safe for local dev and tests.
SERVICEBUS_NAMESPACE = os.environ.get("SERVICEBUS_NAMESPACE", "")
SERVICEBUS_QUEUE_NAME = os.environ.get("SERVICEBUS_QUEUE_NAME", "alarm-intake")
SERVICEBUS_CONSUMER_CONCURRENCY = int(os.environ.get("SERVICEBUS_CONSUMER_CONCURRENCY", "5"))
RECORDING_CALLBACK_TIMEOUT_SECONDS = int(os.environ.get("RECORDING_CALLBACK_TIMEOUT_SECONDS", "45"))

# --- Outcome webhook (push integration for customer tools) ---------------
# When OUTCOME_WEBHOOK_URL is set, every terminal call result is POSTed
# there (signed with X-Callout-Signature when OUTCOME_WEBHOOK_SECRET is
# set). Per-intent `callback_url` overrides the default. Unset ⇒ no push;
# consumers poll GET /api/alarm-status/{alarm_id} or read Cosmos.
OUTCOME_WEBHOOK_URL = os.environ.get("OUTCOME_WEBHOOK_URL", "")
OUTCOME_WEBHOOK_SECRET = os.environ.get("OUTCOME_WEBHOOK_SECRET", "")

# --- Cosmos-backed session store (Redis-free horizontal scale) ----------
# SESSION_COSMOS_CONTAINER selects CosmosSessionStore when REDIS_URL is
# unset — for regions where Azure Cache for Redis is unavailable.
SESSION_COSMOS_CONTAINER = os.environ.get("SESSION_COSMOS_CONTAINER", "")

# ---------------------------------------------------------------------------
# Shared components (initialized at startup)
# ---------------------------------------------------------------------------
sop_engine = SOPEngine()
retry_manager = RetryManager(store=create_retry_store(container_name=RETRY_STATE_COSMOS_CONTAINER))
tenant_registry = TenantRegistry()
sop_catalog = SOPCatalog(registry=tenant_registry)
sop_engine.set_catalog(sop_catalog)
call_handler: CallHandler | None = None
audit_logger: AuditLogger | None = None
foundry_client: FoundryClient | None = None
outcome_notifier: OutcomeNotifier | None = None
queue_client = None  # type: ignore[var-annotated]
intent_consumer: AlarmIntentConsumer | None = None
pending_recording_finalize_tasks: dict[str, asyncio.Task] = {}
MAX_PROCESSED_EVENT_IDS = 200

# Voice-to-voice latency instrumentation (true end-of-speech → response audio).
latency_tracker = LatencyTracker()

# --- Bidirectional media streaming (opt-in low-latency voice path) -------
# When enabled, ACS streams audio to /api/media-stream and we run our own
# Speech continuous recognition (segmentation timeout reachable) + streaming
# TTS. Off by default — the recognize-based flow is unaffected.
STREAMING_MODE = os.environ.get("STREAMING_MODE", "false").lower() in ("1", "true", "yes")
STREAMING_SEGMENTATION_MS = int(os.environ.get("STREAMING_SEGMENTATION_MS", "250"))
STREAMING_BARGE_IN = os.environ.get("STREAMING_BARGE_IN", "false").lower() in ("1", "true", "yes")
STREAMING_GREETING_DELAY_MS = int(os.environ.get("STREAMING_GREETING_DELAY_MS", "1000"))
# Voice engine for the media-streaming path: "chained" (Speech STT->SOP->TTS,
# deterministic), "realtime" (GPT-4o speech-to-speech, lower latency) or
# "voicelive" (managed Voice Live API: gpt-realtime brain + Azure voices +
# semantic VAD / noise suppression, no model deployment needed).
STREAMING_ENGINE = os.environ.get("STREAMING_ENGINE", "chained").lower()
REALTIME_DEPLOYMENT = os.environ.get("REALTIME_DEPLOYMENT", "gpt-realtime")
REALTIME_API_VERSION = os.environ.get("REALTIME_API_VERSION", "2025-04-01-preview")
REALTIME_VOICE = os.environ.get("REALTIME_VOICE", "alloy")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
SPEECH_RESOURCE_ID = os.environ.get("SPEECH_RESOURCE_ID", "")
# Voice Live: a Foundry (AI Services) or Speech resource endpoint — NOT the
# OpenAI account. Model is managed by the service (no deployment/quota).
VOICELIVE_ENDPOINT = os.environ.get("VOICELIVE_ENDPOINT", "")
VOICELIVE_MODEL = os.environ.get("VOICELIVE_MODEL", "gpt-realtime")
VOICELIVE_API_VERSION = os.environ.get("VOICELIVE_API_VERSION", "2026-04-10")
VOICELIVE_VOICE = os.environ.get("VOICELIVE_VOICE", "en-GB-OllieMultilingualNeural")


def _ensure_intent_id(intent: CallIntent) -> str:
    """Return a stable idempotency key for alarm-intent ingestion.

    If the upstream system provides ``intent_id`` we preserve it. Otherwise,
    derive a deterministic UUID from alarm identity + timestamp so webhook
    retries do not fan out duplicate calls under load.
    """
    if intent.intent_id:
        return intent.intent_id

    alarm = intent.alarm
    raw = "|".join(
        [
            alarm.alarm_id,
            alarm.alarm_time,
            alarm.store_id,
            alarm.equipment_name,
            alarm.alarm_type,
        ]
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _normalize_transcript_for_branching(transcript: str, step_id: str) -> str:
    """Apply light-touch normalization for common call-audio ASR mistakes."""
    text = transcript.strip()
    low = text.lower()

    if step_id == "confirm_resolution":
        if "market handle" in low:
            return "mark as handled"
        if re.search(r"\bmark(\s+it)?(\s+as)?\s+handle(d)?\b", low):
            return "mark as handled"
        if re.fullmatch(r"handle(d)?", low):
            return "handled"

    return text


def _cancel_pending_finalize_task(call_id: str) -> None:
    task = pending_recording_finalize_tasks.pop(call_id, None)
    if task is not None and not task.done():
        task.cancel()


def _finalize_disconnected_session(session: CallSession, call_id: str) -> None:
    """Write terminal audit, clean up session, and cancel deferred timers."""
    assert audit_logger is not None
    assert call_handler is not None

    if session.completion_logged:
        return

    audit_logger.log_call_completed(session)
    session.completion_logged = True
    call_handler.remove_session(call_id)
    _cancel_pending_finalize_task(call_id)

    # Push the terminal result to the customer's tools (no-op unless
    # OUTCOME_WEBHOOK_URL or intent.callback_url is configured).
    if outcome_notifier is not None:
        outcome_notifier.notify(session)

    logger.info(
        "Call %s ended — outcome: %s",
        call_id,
        session.outcome.value,
    )


async def _finalize_after_recording_timeout(call_id: str, timeout_seconds: int) -> None:
    """Finalize a disconnected call if no recording URL callback arrives in time."""
    try:
        await asyncio.sleep(timeout_seconds)
        assert call_handler is not None
        session = call_handler.get_session(call_id)
        if session is None:
            return
        if session.ended_at is None:
            return
        _finalize_disconnected_session(session, call_id)
    except asyncio.CancelledError:
        return


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared components on startup, clean up on shutdown."""
    global call_handler, audit_logger, foundry_client, outcome_notifier, queue_client, intent_consumer

    sop_engine.load()
    logger.info("SOP engine loaded: %s", sop_engine.definition.sop_id)

    sop_catalog.load()
    logger.info("SOP catalog loaded: %s", sop_catalog.list_base_sops())

    # Region routing: single "default" region from env unless sops/regions.json
    # (or REGIONS_CONFIG_PATH) defines multiple geographies.
    region_router = create_region_router(
        default_acs_endpoint=ACS_ENDPOINT,
        default_source_phone=ACS_PHONE_NUMBER,
        default_speech_region=SPEECH_REGION,
        default_speech_voice=SPEECH_VOICE,
        default_cognitive_services_endpoint=COGNITIVE_SERVICES_ENDPOINT,
        default_acs_connection_string=ACS_CONNECTION_STRING,
    )

    call_handler = CallHandler(
        acs_endpoint=ACS_ENDPOINT,
        acs_phone_number=ACS_PHONE_NUMBER,
        callback_base_url=CALLBACK_BASE_URL,
        speech_region=SPEECH_REGION,
        speech_voice=SPEECH_VOICE,
        speech_language=ACS_SPEECH_LANGUAGE,
        cognitive_services_endpoint=COGNITIVE_SERVICES_ENDPOINT,
        acs_connection_string=ACS_CONNECTION_STRING,
        end_silence_timeout=ACS_END_SILENCE_TIMEOUT,
        end_silence_timeout_fast=ACS_END_SILENCE_TIMEOUT_FAST,
        initial_silence_timeout=ACS_INITIAL_SILENCE_TIMEOUT,
        interrupt_prompt=ACS_INTERRUPT_PROMPT,
        enable_dtmf_fallback=ACS_ENABLE_DTMF_FALLBACK,
        speech_rate=SPEECH_RATE,
        region_router=region_router,
        media_streaming_ws_url=(
            (CALLBACK_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
             + "/api/media-stream")
            if STREAMING_MODE else ""
        ),
        session_store=create_session_store(
            redis_url=REDIS_URL,
            cosmos_endpoint=COSMOS_ENDPOINT,
            cosmos_container=SESSION_COSMOS_CONTAINER,
        ),
    )

    audit_logger = AuditLogger(cosmos_endpoint=COSMOS_ENDPOINT)
    foundry_client = FoundryClient(agent_endpoint=FOUNDRY_AGENT_ENDPOINT)
    outcome_notifier = OutcomeNotifier(
        webhook_url=OUTCOME_WEBHOOK_URL,
        secret=OUTCOME_WEBHOOK_SECRET,
    )

    queue_client = create_queue_client(
        namespace=SERVICEBUS_NAMESPACE,
        queue_name=SERVICEBUS_QUEUE_NAME,
    )
    if queue_client.enabled:
        intent_consumer = AlarmIntentConsumer(
            namespace=SERVICEBUS_NAMESPACE,
            queue_name=SERVICEBUS_QUEUE_NAME,
            handler=_process_intent,
            max_concurrent=SERVICEBUS_CONSUMER_CONCURRENCY,
        )
        intent_consumer.start()

    logger.info("Call-Out Orchestrator started")
    yield

    if intent_consumer is not None:
        await intent_consumer.stop()

    for task in list(pending_recording_finalize_tasks.values()):
        if not task.done():
            task.cancel()
    pending_recording_finalize_tasks.clear()

    if queue_client is not None:
        await queue_client.close()
    if outcome_notifier is not None:
        await outcome_notifier.close()
    if foundry_client:
        await foundry_client.close()
    logger.info("Call-Out Orchestrator shut down")


app = FastAPI(
    title="Call-Out Agent Orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)

# Serve pre-rendered prompt audio so ACS can fetch it via FileSource. Present
# only when scripts/prerender_prompts.py has produced the files.
_PRERENDER_DIR = Path(__file__).resolve().parent.parent.parent / "prerendered"
if _PRERENDER_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/audio", StaticFiles(directory=str(_PRERENDER_DIR)), name="audio")
    logger.info("Serving pre-rendered prompt audio from %s at /audio", _PRERENDER_DIR)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "sop": sop_engine.definition.sop_id}


@app.get("/metrics/latency")
async def latency_metrics():
    """Voice-to-voice latency stats (ms) vs the <800ms target. Tracks P95."""
    return latency_tracker.stats()


# ---------------------------------------------------------------------------
# Integration surface — status polling for customer tools
# ---------------------------------------------------------------------------
@app.get("/api/call-status/{call_id}", dependencies=[Depends(require_api_key)])
async def call_status(call_id: str):
    """Live snapshot of an in-flight call (404 once the call has finalized —
    use /api/alarm-status or the outcome webhook for terminal results)."""
    assert call_handler is not None
    session = call_handler.get_session(call_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="No active session — call finished or unknown. See /api/alarm-status/{alarm_id}.",
        )
    return {
        "call_id": session.call_id,
        "intent_id": session.intent.intent_id,
        "alarm_id": session.intent.alarm.alarm_id,
        "current_step": session.current_step,
        "attempt": session.attempt_number,
        "outcome": session.outcome.value if session.outcome else None,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
    }


@app.get("/api/alarm-status/{alarm_id}", dependencies=[Depends(require_api_key)])
async def alarm_status(alarm_id: str):
    """Retry/outcome state for an alarm — the poll-based twin of the
    outcome webhook. Backed by the shared retry store (Cosmos in prod)."""
    record = retry_manager.get_record(alarm_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No call-out state for alarm {alarm_id}")
    return {
        "alarm_id": record.alarm_id,
        "attempts": record.attempts,
        "attempt_count": record.attempt_count,
        "max_retries": record.max_retries,
        "final_outcome": record.final_outcome.value if record.final_outcome else None,
        "will_retry": record.should_retry,
    }


def _make_on_terminal(session: "CallSession"):
    """Return an async callback the streaming worker invokes once the SOP
    reaches a terminal turn: stop any recording and hang up the ACS call so
    it doesn't sit in silence waiting on the callee. In streaming mode
    ``server_call_id`` still holds the ACS callConnectionId from initiate."""
    import asyncio

    async def _on_terminal() -> None:
        assert call_handler is not None
        connection_id = session.server_call_id
        if not connection_id:
            logger.warning("No connection id to hang up call %s", session.call_id)
            return
        if session.recording_id:
            await asyncio.to_thread(call_handler.stop_recording, session.recording_id)
        await asyncio.to_thread(call_handler.hang_up, connection_id)

    return _on_terminal


# --- s2s session pre-warm (greeting cold-start removal) ---------------------
# The model WebSocket + session.update cost ~0.5-1s. Doing it only when the
# callee answers pushes the whole greeting late; instead we pre-connect while
# the phone is still RINGING and the media-stream route claims the session.
# Unclaimed sessions (no answer / voicemail cut) are closed after a TTL.
PREWARM_TTL_S = 90
_prewarmed_sessions: dict[str, object] = {}


def _build_s2s_session(session: "CallSession", ws_send=None):
    """Build the engine session for STREAMING_ENGINE=realtime|voicelive.
    ws_send can be attached later (pre-warm happens before the ACS WS exists)."""
    if STREAMING_ENGINE == "voicelive":
        from .voicelive_session import VoiceLiveConfig, VoiceLiveSession

        return VoiceLiveSession(
            call_id=session.call_id,
            session=session,
            config=VoiceLiveConfig(
                endpoint=VOICELIVE_ENDPOINT,
                model=VOICELIVE_MODEL,
                api_version=VOICELIVE_API_VERSION,
                voice_name=VOICELIVE_VOICE,
            ),
            ws_send=ws_send,
            on_persist=call_handler.update_session if call_handler else None,
            on_terminal=_make_on_terminal(session),
        )
    from .realtime_session import RealtimeConfig, RealtimeSession

    return RealtimeSession(
        call_id=session.call_id,
        session=session,
        config=RealtimeConfig(
            openai_endpoint=AZURE_OPENAI_ENDPOINT,
            deployment=REALTIME_DEPLOYMENT,
            api_version=REALTIME_API_VERSION,
            voice=REALTIME_VOICE,
        ),
        ws_send=ws_send,
        on_persist=call_handler.update_session if call_handler else None,
        on_terminal=_make_on_terminal(session),
    )


async def _prewarm_s2s_session(session: "CallSession") -> None:
    """Open + configure the model session while the phone rings, then park it
    for the media-stream route to claim. Expires unclaimed after PREWARM_TTL_S."""
    try:
        s2s = _build_s2s_session(session)
        await s2s.prewarm()
        _prewarmed_sessions[session.call_id] = s2s
    except Exception:
        logger.exception("Pre-warm failed for %s (will connect on answer)", session.call_id)
        return
    await asyncio.sleep(PREWARM_TTL_S)
    leftover = _prewarmed_sessions.pop(session.call_id, None)
    if leftover is not None:
        logger.info("Pre-warmed session for %s unclaimed — closing", session.call_id)
        await leftover.close()


@app.websocket("/api/media-stream")
async def media_stream(websocket: WebSocket):
    """ACS bidirectional media-streaming endpoint (opt-in low-latency path).

    ACS connects here when the call is placed with media streaming enabled.
    We run our own Speech continuous recognition + streaming TTS, reusing
    the SOP engine + agent so guardrails/audit are identical.
    """
    from .media_streaming import (
        MediaStreamingSession,
        StreamingConfig,
        StreamingConversation,
    )

    await websocket.accept()
    call_id = websocket.query_params.get("callId", "")
    assert call_handler is not None and foundry_client is not None

    session = call_handler.get_session(call_id)
    if session is None:
        logger.warning("media-stream: no session for call %s", call_id)
        await websocket.close()
        return

    # --- s2s engines (realtime / voicelive): claim the pre-warmed session ----
    if STREAMING_ENGINE in ("realtime", "voicelive"):
        s2s = _prewarmed_sessions.pop(call_id, None)
        if s2s is not None:
            logger.info(
                "media-stream: using %s engine for call %s (pre-warmed)",
                STREAMING_ENGINE.upper(), call_id,
            )
        else:
            logger.info(
                "media-stream: using %s engine for call %s (cold connect)",
                STREAMING_ENGINE.upper(), call_id,
            )
            s2s = _build_s2s_session(session)
        s2s._acs_send = websocket.send_text  # ACS WS only exists now
        try:
            await s2s.run(websocket)
        except WebSocketDisconnect:
            logger.info("media-stream (%s) disconnected for call %s", STREAMING_ENGINE, call_id)
        except Exception:
            logger.exception("media-stream (%s) error for call %s", STREAMING_ENGINE, call_id)
        return

    conversation = StreamingConversation(
        sop_engine=sop_engine,
        session=session,
        reason_fn=foundry_client.reason,
    )
    config = StreamingConfig(
        speech_region=SPEECH_REGION,
        speech_voice=SPEECH_VOICE,
        speech_language=ACS_SPEECH_LANGUAGE,
        segmentation_silence_ms=STREAMING_SEGMENTATION_MS,
        speech_resource_id=SPEECH_RESOURCE_ID,
        enable_barge_in=STREAMING_BARGE_IN,
        greeting_delay_s=STREAMING_GREETING_DELAY_MS / 1000.0,
    )
    streamer = MediaStreamingSession(
        call_id=call_id,
        conversation=conversation,
        config=config,
        ws_send=websocket.send_text,
        latency_tracker=latency_tracker,
        on_persist=call_handler.update_session,
        on_terminal=_make_on_terminal(session),
        on_warmup=foundry_client.health_check,
    )
    try:
        await streamer.run(websocket)
    except WebSocketDisconnect:
        logger.info("media-stream disconnected for call %s", call_id)
    except Exception:
        logger.exception("media-stream error for call %s", call_id)
        await websocket.close()


# ---------------------------------------------------------------------------
# Alarm intent intake
# ---------------------------------------------------------------------------
@app.post("/api/alarm-intent", dependencies=[Depends(require_api_key)])
async def receive_alarm_intent(intent: CallIntent):
    """Receive a CALL intent from the alarm system.

    Two paths:

    * **Queue configured** (``SERVICEBUS_NAMESPACE`` set): enqueue and
      return 202 ``queued`` immediately. The background consumer drains
      the queue and runs :func:`_process_intent`. This is the
      production path \u2014 absorbs bursts of thousands of alarms
      without blocking the alarm system's HTTP timeout.
    * **No queue**: run :func:`_process_intent` synchronously. Used in
      local dev, tests, and small single-replica deployments.
    """
    assert call_handler is not None

    intent.intent_id = _ensure_intent_id(intent)

    if queue_client is not None and queue_client.enabled:
        await queue_client.enqueue_intent(intent)
        return JSONResponse(
            status_code=202,
            content={"status": "queued", "intent_id": intent.intent_id},
        )

    return await _process_intent(intent)


async def _process_intent(intent: CallIntent):
    """Resolve contact, check retry policy, and place the outbound call.

    Shared entry point for both the synchronous HTTP path and the
    background queue consumer. Returns the same payload the original
    HTTP endpoint did so existing tests keep working.
    """
    assert call_handler is not None
    assert audit_logger is not None

    # Resolve phone number from tenant registry if not provided
    if not intent.phone_number:
        contact = tenant_registry.resolve_primary(
            sop_id=intent.sop_id,
            customer_id=intent.resolved_customer_id,
            store_id=intent.alarm.store_id,
        )
        if contact is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No on-call contact found for customer {intent.resolved_customer_id} / "
                    f"SOP {intent.sop_id} / store {intent.alarm.store_id}"
                ),
            )
        intent.phone_number = contact.phone_number
        logger.info(
            "Resolved contact for customer %s / SOP %s / store %s: %s (%s)",
            intent.resolved_customer_id,
            intent.sop_id,
            intent.alarm.store_id,
            contact.name,
            contact.phone_number,
        )

    # Check retry policy
    should_call, reason = retry_manager.should_attempt_call(intent)
    if not should_call:
        logger.info("Skipping call for alarm %s: %s", intent.alarm.alarm_id, reason)
        return JSONResponse(
            status_code=200,
            content={"status": "skipped", "reason": reason},
        )

    # Create call session
    attempt = retry_manager.get_next_attempt_number(intent.alarm.alarm_id)
    session = CallSession(
        call_id=str(uuid.uuid4()),
        intent=intent,
        attempt_number=attempt,
    )

    # Register session and log
    call_handler.register_session(session)
    audit_logger.log_call_initiated(session)

    # Place outbound call
    try:
        connection_id = call_handler.initiate_call(session)
        session.server_call_id = connection_id
    except Exception as exc:
        logger.exception("Failed to initiate call for alarm %s", intent.alarm.alarm_id)
        retry_manager.record_outcome(intent.alarm.alarm_id, CallOutcome.FAILED)
        raise HTTPException(status_code=502, detail=f"Failed to place call: {exc}")

    # Pre-warm the s2s model session while the phone rings so the greeting
    # isn't delayed by the model connect when the callee answers.
    if STREAMING_MODE and STREAMING_ENGINE in ("realtime", "voicelive"):
        asyncio.create_task(_prewarm_s2s_session(session))

    return {
        "status": "call_initiated",
        "call_id": session.call_id,
        "attempt": attempt,
    }


# ---------------------------------------------------------------------------
# ACS Call Automation webhook
# ---------------------------------------------------------------------------
@app.post("/api/acs-callback")
async def acs_callback(request: Request):
    """Handle ACS Call Automation event callbacks.

    ACS sends events for call state changes:
    - CallConnected → play greeting (first SOP step)
    - RecognizeCompleted → process speech, invoke agent, play next step
    - RecognizeFailed → retry or escalate
    - PlayCompleted → start recognition if step is 'ask'
    - CallDisconnected → log outcome
    """
    assert call_handler is not None
    assert audit_logger is not None
    assert foundry_client is not None

    call_id = request.query_params.get("callId", "")
    session = call_handler.get_session(call_id)

    events = await request.json()
    if not isinstance(events, list):
        events = [events]

    logger.warning("ACS callback received for call %s: %d event(s), types=%s",
                call_id, len(events), [e.get("type", "?") for e in events])

    for event in events:
        event_type = event.get("type", "")

        # Re-bind this call's connection to its region so any replica
        # operates the connection with the correct ACS client (the
        # region was chosen at initiate_call and persisted on the session).
        if session is not None and session.region_id:
            conn_id = event.get("data", {}).get("callConnectionId", "")
            call_handler.ensure_connection_region(conn_id, session.region_id)

        # ACS callbacks are delivered at-least-once. Use the event ID as
        # an idempotency key and persist it in the call session so duplicate
        # webhook deliveries across replicas are ignored safely.
        event_id = event.get("id")
        if session is not None and isinstance(event_id, str) and event_id:
            if event_id in session.processed_event_ids:
                logger.info("Skipping duplicate ACS event for call %s: %s", call_id, event_id)
                continue
            session.processed_event_ids.append(event_id)
            if len(session.processed_event_ids) > MAX_PROCESSED_EVENT_IDS:
                session.processed_event_ids = session.processed_event_ids[-MAX_PROCESSED_EVENT_IDS:]
            call_handler.update_session(session)

        if "CallConnected" in event_type:
            if STREAMING_MODE:
                # The media-streaming worker greets and drives the turns; the
                # recognize/play webhook path must stay out of the way.
                logger.info("CallConnected for %s (streaming mode — worker drives the call)", call_id)
            else:
                await _handle_call_connected(event, session, call_id)

        elif "RecognizeCompleted" in event_type:
            # Latency: mark true end-of-speech ≈ now − end_silence used for
            # the step being recognized (before the handler transitions it).
            if session is not None:
                step = sop_engine.get_current_step(session)
                silence = max(1, round(sop_engine.recognize_timeout_for(
                    step, ACS_END_SILENCE_TIMEOUT_FAST, ACS_END_SILENCE_TIMEOUT)))
                latency_tracker.recognize_completed(call_id, step.id, float(silence))
            await _handle_recognize_completed(event, session, call_id)

        elif "PlayStarted" in event_type:
            # Latency: the agent's response audio has begun.
            latency_tracker.response_audio_started(call_id)

        elif "RecognizeFailed" in event_type:
            await _handle_recognize_failed(event, session, call_id)

        elif "PlayCompleted" in event_type:
            await _handle_play_completed(event, session, call_id)

        elif "RecordingFileStatusUpdated" in event_type or "RecordingStateChanged" in event_type:
            await _handle_recording_file_status_updated(event, session, call_id)

        elif "CallDisconnected" in event_type:
            await _handle_call_disconnected(event, session, call_id)

    return JSONResponse(status_code=200, content={"status": "ok"})


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


async def _handle_call_connected(event: dict, session: CallSession | None, call_id: str):
    assert call_handler is not None
    assert audit_logger is not None

    if session is None:
        logger.warning("CallConnected for unknown session %s", call_id)
        return

    connection_id = event.get("data", {}).get("callConnectionId", "")
    session.server_call_id = event.get("data", {}).get("serverCallId", "") or connection_id
    audit_logger.log_call_connected(session)

    # Play the first SOP step (greeting)
    step = sop_engine.get_current_step(session)
    utterance = sop_engine.render_text(step, session)

    session.conversation_history.append({"role": "agent", "text": utterance})
    audit_logger.log_sop_step(session, utterance)

    # Persist session mutations so a different replica can serve the next webhook
    call_handler.update_session(session)

    if step.type == SOPStepType.SPEAK and step.next:
        # Greeting is speak-only — play it, then advance
        call_handler.play_text(connection_id, utterance, context="sop_speak")
    elif step.type == SOPStepType.ASK:
        # Ask step — play prompt and listen
        call_handler.start_recognize_speech(
            connection_id,
            utterance,
            target_phone=session.intent.phone_number,
            context="sop_ask",
            end_silence_timeout_override=sop_engine.recognize_timeout_for(
                step, ACS_END_SILENCE_TIMEOUT_FAST, ACS_END_SILENCE_TIMEOUT
            ),
        )
    else:
        # Speak with no next (terminal) — shouldn't happen for greeting
        call_handler.play_text(connection_id, utterance, context="sop_terminal")


async def _handle_play_completed(event: dict, session: CallSession | None, call_id: str):
    """After TTS playback finishes, decide what to do next."""
    assert call_handler is not None

    if session is None:
        return

    context = event.get("data", {}).get("operationContext", "")
    connection_id = event.get("data", {}).get("callConnectionId", "")

    if context == "sop_terminal":
        # Terminal step finished playing — hang up
        if session.recording_id:
            call_handler.stop_recording(session.recording_id)
        call_handler.hang_up(connection_id)
        return

    # After speak step, advance to the next step
    current_step = sop_engine.get_current_step(session)
    if current_step.type == SOPStepType.SPEAK and current_step.next:
        # Start recording only after greeting playback completed so recording
        # setup can never block/perturb the transition into recognition.
        if not session.recording_id and session.server_call_id:
            async def _start_recording_background() -> None:
                try:
                    recording_id = await asyncio.to_thread(
                        call_handler.start_recording,
                        session.server_call_id,
                        f"{CALLBACK_BASE_URL}/api/acs-callback?callId={call_id}",
                    )
                    if recording_id:
                        latest = call_handler.get_session(call_id)
                        if latest is not None and not latest.recording_id:
                            latest.recording_id = recording_id
                            call_handler.update_session(latest)
                except Exception:
                    logger.warning("Recording skipped (non-fatal)")

            asyncio.create_task(_start_recording_background())

        session.current_step = current_step.next
        next_step = sop_engine.get_current_step(session)
        utterance = sop_engine.render_text(next_step, session)

        session.conversation_history.append({"role": "agent", "text": utterance})
        # Persist before the next ACS call so a webhook landing on a
        # different replica sees the updated current_step.
        call_handler.update_session(session)

        if next_step.type == SOPStepType.ASK:
            first_turn_barge_in = ACS_FIRST_TURN_BARGE_IN and current_step.id == "greeting"
            call_handler.start_recognize_speech(
                connection_id,
                utterance,
                target_phone=session.intent.phone_number,
                context="sop_ask",
                end_silence_timeout_override=sop_engine.recognize_timeout_for(
                    next_step, ACS_END_SILENCE_TIMEOUT_FAST, ACS_END_SILENCE_TIMEOUT
                ),
                interrupt_prompt_override=(True if first_turn_barge_in else None),
            )
        elif next_step.outcome:
            call_handler.play_text(connection_id, utterance, context="sop_terminal")
        else:
            call_handler.play_text(connection_id, utterance, context="sop_speak")


async def _handle_recognize_completed(event: dict, session: CallSession | None, call_id: str):
    """Process recognized speech and invoke the Foundry agent for reasoning."""
    assert call_handler is not None
    assert audit_logger is not None
    assert foundry_client is not None

    if session is None:
        return

    connection_id = event.get("data", {}).get("callConnectionId", "")

    # Extract transcript from the recognition result. With
    # input_type="speechOrDtmf" the event contains a speechResult AND/OR
    # a dtmfResult — we honour whichever is present, preferring speech
    # when both fired.
    data = event.get("data", {})
    speech_result = data.get("speechResult", {}) or {}
    dtmf_result = data.get("dtmfResult", {}) or {}
    transcript = (speech_result.get("speech") or "").strip()

    if not transcript:
        # No speech — maybe the caller pressed a key. Map common DTMF
        # tones to natural-language equivalents so the Foundry agent
        # processes them through the same branch-matching path. Mapping:
        #   1 = yes / aware / action taken
        #   2 = no / not aware / no action
        #   9 = please connect me with a human (escalate)
        tones = dtmf_result.get("tones") or []
        if tones:
            tone = str(tones[0]).lower()
            dtmf_to_phrase = {
                "one": "yes",
                "two": "no",
                "nine": "please connect me with a human",
                "1": "yes",
                "2": "no",
                "9": "please connect me with a human",
            }
            transcript = dtmf_to_phrase.get(tone, f"keypad input {tone}")
            logger.info(
                "DTMF input on call %s: tone=%s → transcript=%r",
                call_id,
                tone,
                transcript,
            )

    if not transcript:
        # Truly empty turn — treat as unclear
        await _handle_recognize_failed(event, session, call_id)
        return

    transcript = _normalize_transcript_for_branching(transcript, session.current_step)

    logger.info("Speech recognized for call %s: %s", call_id, transcript)
    session.conversation_history.append({"role": "callee", "text": transcript})
    audit_logger.log_speech_recognized(session, transcript)

    # Invoke the Foundry agent for SOP reasoning
    agent_request = AgentRequest(
        alarm_context=session.intent.alarm.model_dump(),
        sop_definition=sop_engine.definition_for(session).model_dump(),
        conversation_history=session.conversation_history,
        current_step=session.current_step,
        customer_id=session.intent.resolved_customer_id,
        unclear_count=session.unclear_count,
    )

    agent_response = await foundry_client.reason(agent_request)
    audit_logger.log_agent_response(session, agent_response.next_step, agent_response.reasoning)

    # Advance the SOP
    next_step, outcome = sop_engine.advance_session(session, agent_response)

    if agent_response.should_escalate:
        audit_logger.log_escalation(session, agent_response.reasoning)

    if next_step is None or outcome is not None:
        # Terminal state — play final utterance and hang up
        utterance = agent_response.utterance
        session.conversation_history.append({"role": "agent", "text": utterance})
        audit_logger.log_sop_step(session, utterance)
        call_handler.update_session(session)
        call_handler.play_text(connection_id, utterance, context="sop_terminal")
    else:
        # Continue the conversation
        utterance = sop_engine.render_text(next_step, session)
        session.conversation_history.append({"role": "agent", "text": utterance})
        audit_logger.log_sop_step(session, utterance)
        call_handler.update_session(session)

        if next_step.type == SOPStepType.ASK:
            call_handler.start_recognize_speech(
                connection_id,
                utterance,
                target_phone=session.intent.phone_number,
                context="sop_ask",
                end_silence_timeout_override=sop_engine.recognize_timeout_for(
                    next_step, ACS_END_SILENCE_TIMEOUT_FAST, ACS_END_SILENCE_TIMEOUT
                ),
            )
        else:
            call_handler.play_text(connection_id, utterance, context="sop_speak")


async def _handle_recognize_failed(event: dict, session: CallSession | None, call_id: str):
    """Handle failed speech recognition — retry or escalate."""
    assert call_handler is not None
    assert audit_logger is not None

    if session is None:
        return

    # Log the ACS failure reason so we can distinguish "caller said
    # nothing" from "barge-in cut the prompt" from "STT misfired" when
    # diagnosing live calls. Common subCodes:
    #   8510 — InitialSilenceTimeout (no speech detected at all)
    #   8511 — InterToneTimeout
    #   8512 — MaxDigitsReceived
    #   8531 — SpeechOptionInvalid
    #   8532 — StopToneDetected (success-ish, # pressed)
    data = event.get("data", {}) or {}
    result_info = data.get("resultInformation", {}) or {}
    logger.warning(
        "RecognizeFailed on call %s step=%s: code=%s subCode=%s message=%r",
        call_id,
        session.current_step,
        result_info.get("code"),
        result_info.get("subCode"),
        result_info.get("message"),
    )

    connection_id = data.get("callConnectionId", "")
    sop_engine.increment_unclear(session)

    if sop_engine.should_escalate_unclear(session):
        # Too many unclear responses — escalate
        audit_logger.log_escalation(session, "Max unclear responses exceeded")
        session.current_step = "escalate_unclear"
        step = sop_engine.get_current_step(session)
        utterance = sop_engine.render_text(step, session)
        session.outcome = CallOutcome.ESCALATE_HUMAN
        session.conversation_history.append({"role": "agent", "text": utterance})
        call_handler.update_session(session)
        call_handler.play_text(connection_id, utterance, context="sop_terminal")
    else:
        # Retry recognition with a clarification prompt. We give the
        # caller two options here: repeat their answer (speech) or press
        # a key. DTMF is the safety net when STT keeps misfiring —
        # without it, callers with strong accents or noisy lines get
        # stuck in a recognition loop until escalation.
        call_handler.update_session(session)
        # Clarification prompt is by construction yes/no/escalate, so
        # use the fast end-silence timeout regardless of which SOP step
        # we're retrying.
        call_handler.start_recognize_speech(
            connection_id,
            (
                "I'm sorry, I didn't quite catch that. Please answer with "
                "yes or no \u2014 or, if you prefer, press 1 for yes, "
                "2 for no, or 9 to speak with a human."
            ),
            target_phone=session.intent.phone_number,
            context="sop_ask",
            end_silence_timeout_override=ACS_END_SILENCE_TIMEOUT_FAST,
        )


async def _handle_call_disconnected(event: dict, session: CallSession | None, call_id: str):
    """Handle call disconnection — record outcome and trigger retry if needed."""
    assert audit_logger is not None
    assert call_handler is not None

    if session is None:
        return

    if session.ended_at is not None:
        return

    from datetime import datetime, timezone

    session.ended_at = datetime.now(timezone.utc).isoformat()

    if session.outcome is None:
        session.outcome = CallOutcome.NO_ANSWER

    audit_logger.log_call_completed(session)

    # Record outcome for retry logic
    retry_manager.record_outcome(
        session.intent.alarm.alarm_id,
        session.outcome,
    )

    # If recording is active but URL has not arrived yet, keep session alive
    # for a short period to absorb late recording callbacks.
    if session.recording_id and not session.recording_url:
        call_handler.update_session(session)
        _cancel_pending_finalize_task(call_id)
        pending_recording_finalize_tasks[call_id] = asyncio.create_task(
            _finalize_after_recording_timeout(call_id, RECORDING_CALLBACK_TIMEOUT_SECONDS)
        )
        logger.info(
            "Call %s disconnected; waiting up to %ss for recording URL callback",
            call_id,
            RECORDING_CALLBACK_TIMEOUT_SECONDS,
        )
        return

    _finalize_disconnected_session(session, call_id)


async def _handle_recording_file_status_updated(
    event: dict,
    session: CallSession | None,
    call_id: str,
):
    """Capture recording URL from ACS recording callback payload."""
    assert call_handler is not None
    assert audit_logger is not None

    if session is None:
        logger.warning("Recording callback for unknown session %s", call_id)
        return

    data = event.get("data", {}) or {}
    recording_id = data.get("recordingId")
    if isinstance(recording_id, str) and recording_id:
        session.recording_id = recording_id

    recording_url = _extract_recording_url(data)
    if recording_url:
        session.recording_url = recording_url
        audit_logger.log_recording(session, recording_url)
        logger.info("Recording file available for call %s", call_id)

        # If the call has already disconnected, finalize immediately now
        # that we have the recording URL.
        if session.ended_at is not None:
            _finalize_disconnected_session(session, call_id)
            return
    else:
        logger.warning("Recording callback received for call %s without content URL", call_id)

    call_handler.update_session(session)
