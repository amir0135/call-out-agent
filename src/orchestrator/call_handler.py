"""ACS Call Automation handler — places and manages outbound calls."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from azure.communication.callautomation import (
    CallAutomationClient,
    CallInvite,
    DtmfTone,
    PhoneNumberIdentifier,
)
from azure.communication.callautomation import (
    AudioFormat,
    FileSource,
    MediaStreamingAudioChannelType,
    MediaStreamingContentType,
    MediaStreamingOptions,
    SsmlSource,
    StreamingTransportType,
    TextSource,
)
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential

from .models import AuditEventType, CallIntent, CallOutcome, CallSession
from .region_router import RegionConfig, RegionRouter, create_region_router
from .session_store import InMemorySessionStore, SessionStore

logger = logging.getLogger(__name__)


class CallHandler:
    """Manages outbound calls via Azure Communication Services Call Automation."""

    def __init__(
        self,
        acs_endpoint: str,
        acs_phone_number: str,
        callback_base_url: str,
        speech_region: str = "eastus",
        speech_voice: str = "en-US-JennyNeural",
        speech_language: str = "en-US",
        cognitive_services_endpoint: str = "",
        acs_connection_string: str = "",
        # --- Latency tuning ---------------------------------------------
        # end_silence_timeout: seconds of silence after the caller stops
        # speaking before STT finalizes. This is the #1 driver of
        # perceived latency on a turn, AND of truncated answers if it's
        # set too low — callers naturally pause mid-sentence ("uh...
        # yeah I'm aware of it"). 1.5s is the sweet spot: it still cuts
        # ~1.5s of dead air vs. the original 3s default, but is generous
        # enough that we don't chop off real answers.
        end_silence_timeout: float = 1.5,
        # end_silence_timeout_fast: shorter timeout used by callers via
        # ``start_recognize_speech(..., end_silence_timeout_override=...)``
        # for prompts whose answer is known to be short (yes/no, single
        # digit). The orchestrator picks per-step based on the SOP
        # ``expected_response`` hint.
        end_silence_timeout_fast: float = 0.8,
        # initial_silence_timeout: how long to wait for the caller to
        # start speaking before declaring RecognizeFailed. Keep this
        # generous (5s) so brief hesitation doesn't trip the failure
        # path.
        initial_silence_timeout: float = 5.0,
        # interrupt_prompt: allow the caller to barge in over the TTS
        # prompt instead of having to wait for it to finish.
        interrupt_prompt: bool = True,
        # --- Recognition robustness --------------------------------------
        # enable_dtmf_fallback: when True, recognition accepts EITHER
        # speech OR a DTMF keypress. This is a critical fallback when
        # STT mishears the caller (poor line quality, accent, background
        # noise) — the prompt can offer "...or press 1 for yes, 2 for
        # no" and the caller always has a way through.
        enable_dtmf_fallback: bool = True,
        # dtmf_max_tones: how many digits to collect per turn. 1 covers
        # all our branch decisions (yes/no/escalate → 1/2/9).
        dtmf_max_tones: int = 1,
        # --- TTS pacing -------------------------------------------------
        # speech_rate: optional SSML <prosody rate="..."> applied to all
        # TTS playback. Useful values:
        #   None / ""   — default voice cadence (no SSML wrapping)
        #   "+5%"       — ~5% snappier, still natural
        #   "+10%"      — noticeably faster, may sound slightly rushed
        #   "medium" / "fast" / "slow" — SSML named rates
        # This is a no-regret latency lever once we hear the call: it
        # shortens every utterance proportionally with no SDK/API risk.
        speech_rate: str | None = None,
        # region_router: routes each call to the ACS resource, caller-ID
        # number, and Speech region nearest the callee. When omitted, a
        # single "default" region is built from the args above so existing
        # single-region deployments behave exactly as before.
        region_router: RegionRouter | None = None,
        # media_streaming_ws_url: when set (wss://.../api/media-stream),
        # outbound calls open a bidirectional audio stream to that worker
        # instead of using the recognize/play actions. Opt-in low-latency path.
        media_streaming_ws_url: str = "",
        # session_store: pluggable storage for active CallSession objects.
        # Defaults to an in-process dict for backward compatibility / tests.
        # In production set REDIS_URL and inject a RedisSessionStore so
        # webhooks landing on any replica can serve any call.
        session_store: SessionStore | None = None,
    ):
        self._acs_endpoint = acs_endpoint
        self._source_phone = acs_phone_number
        self._callback_base_url = callback_base_url.rstrip("/")
        self._speech_region = speech_region
        self._speech_voice = speech_voice
        self._speech_language = speech_language
        self._cognitive_services_endpoint = cognitive_services_endpoint
        self._end_silence_timeout = end_silence_timeout
        self._end_silence_timeout_fast = end_silence_timeout_fast
        self._initial_silence_timeout = initial_silence_timeout
        self._interrupt_prompt = interrupt_prompt
        self._enable_dtmf_fallback = enable_dtmf_fallback
        self._dtmf_max_tones = dtmf_max_tones
        self._speech_rate = speech_rate or None
        self._media_streaming_ws_url = media_streaming_ws_url.rstrip("/")

        # Region routing. Default to a single region from the constructor
        # args for backward compatibility (and unit tests).
        self._router: RegionRouter = region_router or create_region_router(
            default_acs_endpoint=acs_endpoint,
            default_source_phone=acs_phone_number,
            default_speech_region=speech_region,
            default_speech_voice=speech_voice,
            default_cognitive_services_endpoint=cognitive_services_endpoint,
            default_acs_connection_string=acs_connection_string,
        )
        if self._router.is_multi_region:
            logger.info(
                "CallHandler: multi-region routing across %s", self._router.region_ids
            )
        # Lazily-built ACS clients, one per region_id.
        self._clients: dict[str, CallAutomationClient] = {}
        # connection_id -> region_id, so play/recognize/recording/hang-up
        # operate the connection with the same ACS client that created it.
        self._connection_region: dict[str, str] = {}

        # Pluggable session store — in-memory by default for backward
        # compatibility. Inject a RedisSessionStore in production.
        self._sessions: SessionStore = session_store or InMemorySessionStore()

        # Pre-rendered audio for fixed prompts: map of exact prompt text ->
        # public audio URL. Playing a file (~0.3s first byte) is far faster
        # than live TTS (~2.5s). Built from prerendered/manifest.json; empty
        # when absent so we always fall back to live synthesis.
        self._prerendered: dict[str, str] = {}
        self._prerendered_voice: str = ""
        self._load_prerendered_manifest()

    def _load_prerendered_manifest(self) -> None:
        manifest_path = Path(__file__).resolve().parent.parent.parent / "prerendered" / "manifest.json"
        if not manifest_path.exists():
            return
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read pre-rendered manifest: %s", exc)
            return
        self._prerendered_voice = data.get("voice", "")
        # Audio is fetched by ACS, so it must be hosted CLOSE to the ACS
        # region (e.g. Blob/CDN in the same region) for the latency win to
        # materialise. PRERENDER_AUDIO_BASE_URL lets prod point at that;
        # otherwise we serve from the orchestrator's own /audio route.
        base = os.environ.get("PRERENDER_AUDIO_BASE_URL", "").rstrip("/") or f"{self._callback_base_url}/audio"
        for text, fname in data.get("prompts", {}).items():
            self._prerendered[text] = f"{base}/{fname}"
        if self._prerendered:
            logger.info("Loaded %d pre-rendered prompt(s) for low-latency playback", len(self._prerendered))

    def _play_source(self, text: str, voice: str | None = None):
        """Return a FileSource for a pre-rendered fixed prompt, else live TTS.

        Only uses the pre-rendered file when the requested voice matches the
        voice the audio was synthesized with (so per-region locale voices
        still get correct live TTS).
        """
        effective_voice = voice or self._speech_voice
        url = self._prerendered.get(text)
        if url and effective_voice == self._prerendered_voice:
            return FileSource(url=url)
        return self._build_text_source(text, voice=voice)

    # ------------------------------------------------------------------
    # Region / client resolution
    # ------------------------------------------------------------------

    def _client_for_region(self, region_id: str) -> CallAutomationClient:
        """Return (and cache) the ACS client for a region."""
        client = self._clients.get(region_id)
        if client is not None:
            return client
        region = self._router.get(region_id)
        if region.acs_connection_string:
            logger.info("CallHandler[%s]: using ACS connection string auth", region.region_id)
            client = CallAutomationClient.from_connection_string(
                region.acs_connection_string
            )
        else:
            logger.info("CallHandler[%s]: using DefaultAzureCredential (AAD) auth", region.region_id)
            client = CallAutomationClient(
                endpoint=region.acs_endpoint,
                credential=DefaultAzureCredential(),
            )
        self._clients[region.region_id] = client
        return client

    def _region_for_connection(self, call_connection_id: str) -> RegionConfig:
        """Resolve the region of an existing connection (default if unknown)."""
        region_id = self._connection_region.get(call_connection_id)
        return self._router.get(region_id)

    def _client_for_connection(self, call_connection_id: str) -> CallAutomationClient:
        return self._client_for_region(self._region_for_connection(call_connection_id).region_id)

    def ensure_connection_region(self, call_connection_id: str, region_id: str | None) -> None:
        """Record a connection's region (e.g. after a session loads on a new
        replica) so subsequent operations use the correct ACS client."""
        if call_connection_id and region_id:
            self._connection_region[call_connection_id] = region_id

    # ------------------------------------------------------------------
    # Outbound call initiation
    # ------------------------------------------------------------------

    def initiate_call(self, session: CallSession) -> str:
        """Place an outbound call and return the call connection ID.

        Routes the call to the ACS resource, caller-ID number, and Speech
        region nearest the callee (by E.164 dial code), and records the
        chosen region on the session + connection map so every later
        operation uses the same ACS client.
        """
        region = self._router.resolve(session.intent.phone_number)
        session.region_id = region.region_id
        client = self._client_for_region(region.region_id)

        target = PhoneNumberIdentifier(session.intent.phone_number)
        source = PhoneNumberIdentifier(region.source_phone)
        callback_url = f"{self._callback_base_url}/api/acs-callback?callId={session.call_id}"

        logger.info(
            "Initiating outbound call to %s for alarm %s via region '%s' (caller-id %s)",
            session.intent.phone_number,
            session.intent.alarm.alarm_id,
            region.region_id,
            region.source_phone,
        )

        cognitive_endpoint = region.cognitive_services_endpoint or self._cognitive_services_endpoint
        media_streaming = None
        if self._media_streaming_ws_url:
            # Bidirectional audio stream to our own STT/TTS worker (per-call
            # callId so the worker can load the right session).
            transport_url = f"{self._media_streaming_ws_url}?callId={session.call_id}"
            media_streaming = MediaStreamingOptions(
                transport_url=transport_url,
                transport_type=StreamingTransportType.WEBSOCKET,
                content_type=MediaStreamingContentType.AUDIO,
                audio_channel_type=MediaStreamingAudioChannelType.MIXED,
                start_media_streaming=True,
                enable_bidirectional=True,
                audio_format=AudioFormat.PCM16_K_MONO,
            )
            logger.info("Call %s will use bidirectional media streaming", session.call_id)
            # Pre-warm the Cognitive Services token now, while the call is
            # still ringing. The streaming worker reuses this module-level
            # cache on CallConnected, taking the ~0.9s token fetch off the
            # critical path before the greeting.
            try:
                import threading

                from .media_streaming import _cached_cognitive_token

                threading.Thread(target=_cached_cognitive_token, daemon=True).start()
            except Exception:
                logger.debug("Token pre-warm skipped", exc_info=True)
        result = client.create_call(
            target_participant=CallInvite(target=target),
            source_caller_id_number=source,
            callback_url=callback_url,
            cognitive_services_endpoint=cognitive_endpoint or None,
            media_streaming=media_streaming,
        )

        connection_id = result.call_connection_id
        self._connection_region[connection_id] = region.region_id
        logger.info("Call connection created: %s", connection_id)
        return connection_id

    # ------------------------------------------------------------------
    # TTS playback
    # ------------------------------------------------------------------

    def _build_text_source(self, text: str, voice: str | None = None) -> TextSource | SsmlSource:
        """Return a TextSource by default, or an SsmlSource when speech_rate is set.

        ACS Call Automation has two distinct play-source types:
          * ``TextSource`` — plain text + voice_name; ACS adds default prosody.
          * ``SsmlSource``  — caller-provided SSML document; voice is declared
                              inside ``<voice name="...">`` instead of a kwarg.

        We only build SSML when ``speech_rate`` is configured so the default
        path is unchanged — zero risk of breaking existing voices. ``voice``
        overrides the default voice (used for per-region locale voices).
        """
        voice_name = voice or self._speech_voice
        # Keep the fastest TextSource path for default playback while still
        # nudging pronunciation for brand names.
        normalized_text = re.sub(r"(?i)\bcontoso\b", "Con-toso", text)
        if not self._speech_rate:
            return TextSource(text=normalized_text, voice_name=voice_name)

        # XML-escape the user-visible text so apostrophes and ampersands
        # in alarm descriptions don't break the SSML document.
        from xml.sax.saxutils import escape as _xml_escape

        escaped_text = _xml_escape(normalized_text)

        # Keep SSML minimal to minimise TTS first-byte latency: a prosody
        # rate (and slightly warmer pitch) only. NO leading/comma/sentence
        # <break>s — they add wall-clock to every prompt and delay the
        # first audio byte, which directly hurts perceived reaction time.
        rate = self._speech_rate or "0%"
        body = f'<prosody rate="{rate}" pitch="-2%">{escaped_text}</prosody>'

        ssml = (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xml:lang="en-US">'
            f'<voice name="{voice_name}">'
            f'{body}'
            '</voice>'
            '</speak>'
        )
        return SsmlSource(ssml_text=ssml)

    def play_text(
        self,
        call_connection_id: str,
        text: str,
        context: str = "",
        region_id: str | None = None,
    ) -> None:
        """Play synthesized speech on an active call (via the call's region client)."""
        region = self._router.get(region_id) if region_id else self._region_for_connection(call_connection_id)
        self.ensure_connection_region(call_connection_id, region.region_id)
        client = self._client_for_region(region.region_id)
        call_connection = client.get_call_connection(call_connection_id)
        source = self._play_source(text, voice=region.speech_voice or None)
        call_connection.play_media(
            play_source=source,
            operation_context=context,
        )
        logger.info("Playing TTS on %s: %s...", call_connection_id, text[:80])

    # ------------------------------------------------------------------
    # STT recognition
    # ------------------------------------------------------------------

    def start_recognize_speech(
        self,
        call_connection_id: str,
        prompt_text: str,
        target_phone: str,
        context: str = "",
        end_silence_timeout_override: float | None = None,
        interrupt_prompt_override: bool | None = None,
        region_id: str | None = None,
    ) -> None:
        """Play a prompt and recognize the caller's speech response.

        When ``enable_dtmf_fallback`` is true (the default), the
        recognizer accepts either speech OR a single DTMF keypress so
        the caller always has a path forward even if STT fails.

        ``end_silence_timeout_override`` lets the orchestrator pick a
        tighter timeout for short-answer prompts (yes/no) without
        affecting open-ended prompts. Pass ``None`` to use the
        instance default.
        """
        # Resolve region: explicit override > known connection mapping >
        # deterministic lookup by callee number (multi-replica safe).
        if region_id:
            region = self._router.get(region_id)
        elif call_connection_id in self._connection_region:
            region = self._region_for_connection(call_connection_id)
        else:
            region = self._router.resolve(target_phone)
        self.ensure_connection_region(call_connection_id, region.region_id)
        client = self._client_for_region(region.region_id)
        call_connection = client.get_call_connection(call_connection_id)
        prompt = self._play_source(prompt_text, voice=region.speech_voice or None)

        effective_silence = (
            end_silence_timeout_override
            if end_silence_timeout_override is not None
            else self._end_silence_timeout
        )

        # ACS expects integer seconds for both silence timeouts. The SDK
        # multiplies end_silence_timeout by 1000 internally, so a float
        # like 0.8 produces 800.0 (float) in the request body and ACS
        # rejects with HTTP 400 schema violation. ``max(1, round(...))``
        # avoids a 0 value (which would disable detection entirely).
        end_silence_int = max(1, round(effective_silence))
        initial_silence_int = max(1, round(self._initial_silence_timeout))
        effective_interrupt_prompt = (
            self._interrupt_prompt
            if interrupt_prompt_override is None
            else interrupt_prompt_override
        )

        recognize_kwargs: dict[str, Any] = {
            "target_participant": PhoneNumberIdentifier(target_phone),
            "play_prompt": prompt,
            "operation_context": context,
            "speech_language": self._speech_language,
            "end_silence_timeout": end_silence_int,
            "initial_silence_timeout": initial_silence_int,
            "interrupt_prompt": effective_interrupt_prompt,
        }

        if self._enable_dtmf_fallback:
            recognize_kwargs["input_type"] = "speechOrDtmf"
            recognize_kwargs["dtmf_max_tones_to_collect"] = self._dtmf_max_tones
            # 5s between tones — user has time to find the key.
            recognize_kwargs["dtmf_inter_tone_timeout"] = 5
            # ``#`` ends the input early so users familiar with IVR
            # menus can confirm immediately. ACS requires the enum value
            # (serialises to ``"pound"``); the literal string ``"#"`` is
            # rejected with HTTP 400 "Error converting value '#' to type
            # ...Tone".
            recognize_kwargs["dtmf_stop_tones"] = [DtmfTone.POUND]
        else:
            recognize_kwargs["input_type"] = "speech"

        call_connection.start_recognizing_media(**recognize_kwargs)
        logger.info(
            "Started %s recognition on %s (end_silence=%ds, initial_silence=%ds, barge_in=%s)",
            recognize_kwargs["input_type"],
            call_connection_id,
            end_silence_int,
            initial_silence_int,
            effective_interrupt_prompt,
        )

    # ------------------------------------------------------------------
    # Call recording
    # ------------------------------------------------------------------

    def start_recording(
        self,
        server_call_id: str,
        recording_state_callback_url: str | None = None,
        region_id: str | None = None,
    ) -> str | None:
        """Start recording the call. Returns recording ID."""
        try:
            client = self._client_for_region(self._router.get(region_id).region_id)
            # ACS SDK 1.4 accepts ``server_call_id`` as a keyword arg and
            # constructs the typed ``CallLocator`` internally. Passing
            # ``call_locator=<str>`` (legacy shape) makes the SDK call
            # ``._to_generated()`` on the string and crash with
            # ``AttributeError: 'str' object has no attribute '_to_generated'``.
            result = client.start_recording(
                server_call_id=server_call_id,
                recording_state_callback_url=recording_state_callback_url,
            )
            recording_id = result.recording_id
            logger.info("Recording started: %s", recording_id)
            return recording_id
        except Exception:
            logger.exception("Failed to start recording")
            return None

    def stop_recording(self, recording_id: str, region_id: str | None = None) -> None:
        """Stop an active recording (best effort)."""
        try:
            client = self._client_for_region(self._router.get(region_id).region_id)
            client.stop_recording(recording_id=recording_id)
            logger.info("Recording stopped: %s", recording_id)
        except ResourceNotFoundError:
            logger.info("Recording %s already stopped or unavailable", recording_id)
        except Exception:
            logger.exception("Failed to stop recording %s", recording_id)

    # ------------------------------------------------------------------
    # Call termination
    # ------------------------------------------------------------------

    def hang_up(self, call_connection_id: str) -> None:
        """Hang up the call.

        Tolerates the common case where ACS has already torn the call
        down (e.g. the callee hung up first, or we finished the final
        prompt and ACS auto-disconnected). In that case ``terminate``
        returns HTTP 404 ``(8522) Call not found`` — not an error
        worth a stack trace.
        """
        try:
            client = self._client_for_connection(call_connection_id)
            call_connection = client.get_call_connection(call_connection_id)
            call_connection.hang_up(is_for_everyone=True)
            logger.info("Call hung up: %s", call_connection_id)
        except ResourceNotFoundError:
            # Call already gone — nothing to hang up.
            logger.info(
                "Call %s already disconnected (no hang-up needed)",
                call_connection_id,
            )
        except Exception:
            logger.exception("Failed to hang up call %s", call_connection_id)

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def register_session(self, session: CallSession) -> None:
        """Register a call session for tracking."""
        self._sessions.put(session)

    def get_session(self, call_id: str) -> CallSession | None:
        """Look up a call session by call ID."""
        return self._sessions.get(call_id)

    def update_session(self, session: CallSession) -> None:
        """Persist mutations to an existing session.

        With an in-memory store this is a no-op (the object is mutated
        in place). With Redis we must write the new JSON back so other
        replicas see the change.
        """
        self._sessions.put(session)

    def remove_session(self, call_id: str) -> CallSession | None:
        """Remove and return a completed call session."""
        return self._sessions.remove(call_id)
