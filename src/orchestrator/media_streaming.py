"""ACS bidirectional media-streaming worker (low-latency voice path).

This is the architecture that can actually hit the <800ms voice-to-voice
target. Instead of ACS's request/response ``recognize`` action (whose
``end_silence_timeout`` floors at 1s), ACS streams raw audio over a
WebSocket to us, and we run our *own* Azure Speech continuous recognition
— where ``Speech_SegmentationSilenceTimeoutMs`` (~250ms) is reachable —
plus streaming TTS, and send audio back over the same socket.

Design goals:
  * Reuse the existing brain: ``foundry_client.reason`` + ``sop_engine``
    so SOP adherence / guardrails / audit are identical to the webhook path.
  * Opt-in: nothing here runs unless STREAMING_MODE is enabled and ACS is
    told to stream. The recognize-based flow is untouched.
  * Testable: the wire protocol and the SOP conversation driver are pure
    and unit-tested; the live Azure Speech loop is lazily imported.

Status: SCAFFOLD. The pure layers (protocol + StreamingConversation) are
complete and tested. ``MediaStreamingSession`` is structurally complete but
must be validated on a live call with ``azure-cognitiveservices-speech``
installed (see requirements). Marked TODO(live) where that applies.

ACS media-streaming wire format (JSON over WebSocket):
  in : {"kind":"AudioMetadata","audioMetadata":{"encoding":"PCM",
        "sampleRate":16000,"channels":1,...}}
  in : {"kind":"AudioData","audioData":{"data":"<base64 pcm>",
        "timestamp":"...","silent":false}}
  out: {"kind":"AudioData","audioData":{"data":"<base64 pcm>"}}  # play
  out: {"kind":"StopAudio","stopAudio":{}}                       # barge-in
"""

from __future__ import annotations

import base64
import json
import re
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .models import AgentRequest, AgentResponse, CallSession, SOPStepType
from .sop_engine import SOPEngine

logger = logging.getLogger("orchestrator.media_streaming")

# ACS streams 16kHz, 16-bit, mono PCM in both directions.
SAMPLE_RATE = 16000
BITS_PER_SAMPLE = 16
CHANNELS = 1
# One outbound frame = 640 bytes = 320 samples @ 16kHz = exactly 20ms of audio.
FRAME_BYTES = 640
FRAME_DURATION_S = 0.020

# Module-level Cognitive Services AAD token cache. Speech setup happens per
# call (and ACS may open several connections at once); without caching, each
# does a fresh ~0.7s az-CLI token fetch, slowing start and causing the WS
# retries we observed. Tokens are valid ~1h; refresh 60s before expiry.
_token_cache: dict = {"token": None, "expires": 0.0}


def _cached_cognitive_token() -> str:
    import os
    import time

    from azure.identity import DefaultAzureCredential

    if _token_cache["token"] and (_token_cache["expires"] - time.time()) > 60:
        return _token_cache["token"]
    # In Azure Container Apps the system-assigned managed identity is the ONLY
    # credential, so it must NOT be excluded. Locally we exclude it to skip the
    # ~2s IMDS probe that otherwise stalls token fetch. CONTAINER_APP_NAME is
    # injected by ACA at runtime.
    in_aca = bool(os.environ.get("CONTAINER_APP_NAME"))
    cred = DefaultAzureCredential(
        exclude_managed_identity_credential=not in_aca,
        exclude_environment_credential=True,
    )
    tok = cred.get_token("https://cognitiveservices.azure.com/.default")
    _token_cache["token"] = tok.token
    _token_cache["expires"] = float(tok.expires_on)
    return tok.token


# ---------------------------------------------------------------------------
# Wire protocol (pure, unit-tested)
# ---------------------------------------------------------------------------
def parse_acs_message(raw: str) -> tuple[str, dict]:
    """Parse an inbound ACS media-streaming frame → (kind, payload)."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "Unknown", {}
    kind = msg.get("kind", "Unknown")
    return kind, msg


def extract_audio_pcm(msg: dict) -> bytes | None:
    """Return decoded PCM bytes from an AudioData frame, or None."""
    data = msg.get("audioData", {})
    b64 = data.get("data")
    if not b64 or data.get("silent") is True:
        return None
    try:
        return base64.b64decode(b64)
    except (ValueError, TypeError):
        return None


def audio_data_out(pcm: bytes) -> str:
    """Build an outbound AudioData frame to play ``pcm`` to the caller.

    NOTE: outbound (server→ACS) uses PascalCase keys and a StopAudio field
    — different from the lowercase inbound (ACS→server) format. Getting this
    wrong means ACS silently ignores the audio (no error, no playback).
    """
    return json.dumps({
        "Kind": "AudioData",
        "AudioData": {"Data": base64.b64encode(pcm).decode("ascii")},
        "StopAudio": None,
    })


def stop_audio_out() -> str:
    """Build a StopAudio frame to cut current playback (barge-in)."""
    return json.dumps({"Kind": "StopAudio", "AudioData": None, "StopAudio": {}})


# Greeting words a caller naturally says when they pick up ("Hello?"). These
# are not answers to the agent's question, so a transcript consisting only of
# these is ignored instead of being treated as an (unclear) reply — otherwise
# the agent wrongly counts it as a failed answer and re-asks/escalates.
_GREETING_TOKENS = {
    "hello", "hallo", "hi", "hey", "hej", "yello", "hullo", "helo", "hallo",
    "goddag", "hejsa", "yo", "hallo",
}


def is_greeting_only(text: str) -> bool:
    """True if ``text`` is only greeting words (a pickup "hello"), not an answer."""
    words = re.findall(r"[a-zA-Z]+", text.lower())
    return bool(words) and all(w in _GREETING_TOKENS for w in words)


# ---------------------------------------------------------------------------
# SOP conversation driver (transport-agnostic, unit-tested)
# ---------------------------------------------------------------------------
@dataclass
class StreamTurn:
    utterances: list[str]
    is_terminal: bool


ReasonFn = Callable[[AgentRequest], Awaitable[AgentResponse]]


class StreamingConversation:
    """Drives the SOP for a streamed call, reusing the agent + sop_engine.

    Mirrors the webhook handler's reason→advance logic so SOP behaviour is
    identical regardless of transport.
    """

    def __init__(self, sop_engine: SOPEngine, session: CallSession, reason_fn: ReasonFn):
        self._sop = sop_engine
        self._session = session
        self._reason = reason_fn

    @property
    def session(self) -> CallSession:
        return self._session

    def opening_turns(self) -> list[str]:
        """The opening utterances: greeting + any following speak steps, up to
        and including the first question (so the agent doesn't go silent)."""
        step = self._sop.get_current_step(self._session)
        utterances, _ = self._speak_chain(step)
        return utterances

    def _speak_chain(self, step) -> tuple[list[str], bool]:
        """Render ``step`` and auto-advance through SPEAK steps until an ASK
        step (wait for the caller) or a terminal step (end the call).

        Returns (utterances, is_terminal).
        """
        utterances: list[str] = []
        current = step
        terminal = False
        while True:
            text = self._sop.render_text(current, self._session)
            self._session.conversation_history.append({"role": "agent", "text": text})
            utterances.append(text)
            if current.type == SOPStepType.ASK:
                break  # wait for the caller's answer
            if current.next:
                self._session.current_step = current.next
                current = self._sop.get_step(current.next)
            else:
                terminal = True  # terminal speak step (no next)
                break
        return utterances, terminal

    async def respond_to(self, transcript: str) -> StreamTurn:
        """Process a recognized caller utterance, return the next agent turn(s)."""
        self._session.conversation_history.append({"role": "callee", "text": transcript})

        request = AgentRequest(
            alarm_context=self._session.intent.alarm.model_dump(),
            sop_definition=self._sop.definition_for(self._session).model_dump(),
            conversation_history=self._session.conversation_history,
            current_step=self._session.current_step,
            customer_id=self._session.intent.resolved_customer_id,
            unclear_count=self._session.unclear_count,
        )
        response = await self._reason(request)
        next_step, outcome = self._sop.advance_session(self._session, response)

        if next_step is None or outcome is not None:
            utterance = response.utterance
            self._session.conversation_history.append({"role": "agent", "text": utterance})
            return StreamTurn(utterances=[utterance], is_terminal=True)

        utterances, terminal = self._speak_chain(next_step)
        return StreamTurn(utterances=utterances, is_terminal=terminal)


# ---------------------------------------------------------------------------
# Live streaming session (Azure Speech; lazily imported)
# ---------------------------------------------------------------------------
@dataclass
class StreamingConfig:
    speech_region: str
    speech_voice: str = "en-US-JennyNeural"
    speech_language: str = "en-US"
    # THE knob the recognize path can't reach. ~200-300ms balances snappy
    # turn-taking against clipping callers mid-pause.
    segmentation_silence_ms: int = 250
    # AAD resource id for the Speech resource (disableLocalAuth=true).
    speech_resource_id: str = ""
    # Barge-in: let the caller interrupt the agent's audio. Off by default —
    # without echo cancellation the recognizer can hear the agent's own TTS
    # and cut it off falsely, leaving dead air. Enable only on clean lines.
    enable_barge_in: bool = False
    # Grace period after the final (terminal) line before we hang up, so the
    # caller hears the whole goodbye drain out of ACS's jitter buffer.
    hangup_delay_s: float = 1.2
    # Pause after the call connects before the agent starts the greeting, so a
    # caller who says "Hello?" on pickup isn't talked over. The recognizer
    # builds during this window, so their greeting is heard (and ignored).
    greeting_delay_s: float = 1.0


WsSend = Callable[[str], Awaitable[None]]


class MediaStreamingSession:
    """Handles one ACS media-streaming WebSocket: audio in → STT → SOP →
    streaming TTS → audio out, with barge-in.

    TODO(live): validate end-to-end with azure-cognitiveservices-speech
    installed and ACS media streaming pointed at the WS route. The Speech
    callbacks run on SDK threads; we marshal back to the event loop.
    """

    def __init__(
        self,
        call_id: str,
        conversation: StreamingConversation,
        config: StreamingConfig,
        ws_send: WsSend,
        latency_tracker=None,
        on_persist: Callable[[CallSession], None] | None = None,
        on_terminal: Callable[[], Awaitable[None]] | None = None,
        on_warmup: Callable[[], Awaitable[object]] | None = None,
    ):
        self._call_id = call_id
        self._convo = conversation
        self._cfg = config
        self._ws_send = ws_send
        self._latency = latency_tracker
        self._on_persist = on_persist
        self._on_terminal = on_terminal
        self._on_warmup = on_warmup
        self._recognizer = None
        self._push_stream = None
        self._synth = None
        self._loop = None
        self._speaking = False  # True while we are streaming TTS out
        self._turn_active = False  # True while a caller turn is being handled

    # --- Azure Speech setup (lazy import so module loads without the SDK) ---
    def _aad_token(self) -> str:
        """Fetch a Cognitive Services AAD token (module-cached, IMDS skipped)."""
        return _cached_cognitive_token()

    def _build_synth(self) -> None:
        """Build the streaming synthesizer only. Fast — no blocking pre-warm.

        The greeting's first ``start_speaking`` call warms the connection
        (~0.8s once); every later turn reuses it (~170ms). Building this
        separately lets us start the greeting ~2s sooner than waiting on the
        recognizer + an explicit ``Connection.open(True)``.
        """
        import azure.cognitiveservices.speech as speechsdk

        auth = f"aad#{self._cfg.speech_resource_id}#{self._aad_token()}"
        scfg = speechsdk.SpeechConfig(auth_token=auth, region=self._cfg.speech_region)
        scfg.speech_synthesis_voice_name = self._cfg.speech_voice
        scfg.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
        )
        self._synth = speechsdk.SpeechSynthesizer(speech_config=scfg, audio_config=None)

    def _build_recognizer(self) -> None:
        """Build the continuous recognizer and start recognition. Runs in a
        worker thread, concurrently with the greeting, so it's listening by
        the time the caller is expected to answer.
        """
        import azure.cognitiveservices.speech as speechsdk

        auth = f"aad#{self._cfg.speech_resource_id}#{self._aad_token()}"
        rcfg = speechsdk.SpeechConfig(auth_token=auth, region=self._cfg.speech_region)
        rcfg.speech_recognition_language = self._cfg.speech_language
        rcfg.set_property(
            speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
            str(self._cfg.segmentation_silence_ms),
        )
        fmt = speechsdk.audio.AudioStreamFormat(
            samples_per_second=SAMPLE_RATE, bits_per_sample=BITS_PER_SAMPLE, channels=CHANNELS
        )
        push = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
        recognizer = speechsdk.SpeechRecognizer(
            speech_config=rcfg, audio_config=speechsdk.audio.AudioConfig(stream=push)
        )
        recognizer.recognizing.connect(self._on_recognizing)
        recognizer.recognized.connect(self._on_recognized)
        recognizer.start_continuous_recognition_async()
        # Publish last so the read loop only writes audio once it's ready.
        self._recognizer = recognizer
        self._push_stream = push

    # --- ACS WebSocket loop ---
    async def run(self, ws) -> None:
        """Read ACS frames until the socket closes.

        Speech setup (token + warm connection) is blocking, so it runs in a
        background thread while we immediately start reading the socket —
        otherwise ACS drops the stream during the ~1-2s setup. Audio frames
        that arrive before the recognizer is ready are dropped (the caller
        is silent during the greeting anyway).
        """
        import asyncio

        self._loop = asyncio.get_running_loop()
        setup = asyncio.create_task(self._setup_and_greet())
        frames = 0
        try:
            async for raw in ws.iter_text():
                frames += 1
                if frames == 1:
                    logger.info("First inbound media frame for %s", self._call_id)
                try:
                    kind, m = parse_acs_message(raw)
                    if kind == "AudioData":
                        if self._push_stream is not None:
                            pcm = extract_audio_pcm(m)
                            if pcm:
                                self._push_stream.write(pcm)
                    elif kind == "AudioMetadata":
                        logger.info("Media stream metadata for %s: %s", self._call_id, m.get("audioMetadata"))
                except Exception:
                    logger.exception("Error handling media frame for %s", self._call_id)
        finally:
            setup.cancel()
            if self._recognizer is not None:
                self._recognizer.stop_continuous_recognition_async()
            if self._push_stream is not None:
                self._push_stream.close()

    async def _setup_and_greet(self) -> None:
        import asyncio

        try:
            # 1. Token first (usually pre-warmed during ringing, so ~instant).
            await asyncio.to_thread(self._aad_token)
            # Warm the agent HTTP connection in the background so the FIRST
            # caller answer isn't slowed by a cold TLS/DNS handshake to the
            # agent service (~190ms otherwise). Greeting gives it plenty of time.
            if self._on_warmup is not None:
                asyncio.create_task(self._safe_warmup())
            # 2. Build only the synthesizer — fast — and start greeting right
            #    away. The greeting's first chunk warms the connection; we do
            #    NOT pay the ~2s blocking pre-warm before the caller hears us.
            await asyncio.to_thread(self._build_synth)
            # 3. Build the recognizer concurrently: it just needs to be
            #    listening by the time the greeting + first question end.
            rec_task = asyncio.create_task(asyncio.to_thread(self._build_recognizer))
            logger.info("Synth ready for %s; greeting", self._call_id)
            # Give the caller a beat to say "Hello?" before we start talking.
            if self._cfg.greeting_delay_s > 0:
                await asyncio.sleep(self._cfg.greeting_delay_s)
            for text in self._convo.opening_turns():
                await self._speak(text)
            await rec_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Streaming setup/greet failed for %s", self._call_id)

    async def _safe_warmup(self) -> None:
        try:
            await self._on_warmup()
        except Exception:
            logger.debug("Agent warmup failed for %s", self._call_id, exc_info=True)

    # --- Speech SDK callbacks (run on SDK threads) ---
    def _on_recognizing(self, evt) -> None:
        # Partial result — the caller may be talking. Only act on this if
        # barge-in is enabled; otherwise echo of our own TTS could cut us off.
        if not self._cfg.enable_barge_in:
            return
        text = getattr(evt.result, "text", "")
        if text and self._speaking:
            self._schedule(self._barge_in())

    def _on_recognized(self, evt) -> None:
        import azure.cognitiveservices.speech as speechsdk

        if evt.result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return
        transcript = (evt.result.text or "").strip()
        if not transcript:
            return
        # Echo guard: the recognizer is always on, so it also hears our own
        # TTS (and the caller talking over the prompt). Ignore anything that
        # lands while we're speaking or already handling a turn — otherwise
        # the agent reacts to itself and the turn state desyncs.
        if self._speaking or self._turn_active:
            logger.info("Ignoring transcript (agent busy) [%s]: %s", self._call_id, transcript)
            return
        # The caller often says "Hello?" when they pick up. That's not an answer
        # to our question, so ignore greeting-only utterances rather than
        # treating them as an unclear reply (which would re-ask or escalate).
        if is_greeting_only(transcript):
            logger.info("Ignoring pickup greeting [%s]: %s", self._call_id, transcript)
            return
        # True end-of-speech ≈ now − segmentation silence (the recognizer
        # waited that long before firing). Feeds the same latency harness.
        if self._latency is not None:
            self._latency.recognize_completed(
                self._call_id,
                self._convo.session.current_step,
                self._cfg.segmentation_silence_ms / 1000.0,
            )
        self._schedule(self._handle_transcript(transcript))

    # --- async helpers marshalled back onto the event loop ---
    def _schedule(self, coro) -> None:
        import asyncio

        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _barge_in(self) -> None:
        self._speaking = False
        await self._ws_send(stop_audio_out())

    async def _handle_transcript(self, transcript: str) -> None:
        if self._turn_active:
            return
        self._turn_active = True
        try:
            logger.info("Streamed transcript [%s]: %s", self._call_id, transcript)
            turn = await self._convo.respond_to(transcript)
            if self._on_persist is not None:
                self._on_persist(self._convo.session)
            for text in turn.utterances:
                await self._speak(text)
            # Terminal turn: caller hears the closing line, then we hang up so
            # the call doesn't linger in silence waiting on the callee.
            if turn.is_terminal and self._on_terminal is not None:
                import asyncio

                await asyncio.sleep(self._cfg.hangup_delay_s)
                logger.info("Conversation complete for %s — hanging up", self._call_id)
                await self._on_terminal()
        finally:
            self._turn_active = False

    async def _speak(self, text: str) -> None:
        """Synthesize ``text`` and stream PCM frames back to ACS.

        Frames are paced to an ABSOLUTE real-time schedule (20ms/frame). A
        fixed per-frame sleep is not enough: a fast voice synthesizes quicker
        than real-time, so a constant <20ms sleep streams audio ahead of ACS's
        jitter buffer, which then drops frames and the caller hears choppy /
        broken speech. Scheduling each frame at start + n*20ms keeps us exactly
        at real-time (never ahead), while still catching up after a synthesis
        stall instead of leaving a gap.
        """
        import asyncio
        import time

        self._speaking = True
        first_frame = True
        frames = 0
        next_at = 0.0
        try:
            async for pcm_frame in self._synthesize_stream(text):
                if not self._speaking:  # barge-in cut us off
                    break
                if first_frame:
                    if self._latency is not None:
                        self._latency.response_audio_started(self._call_id)
                    next_at = time.monotonic()
                    first_frame = False
                await self._ws_send(audio_data_out(pcm_frame))
                frames += 1
                # Pace to real-time without bursting. Each frame is due 20ms
                # after the previous. If synthesis stalled and we fell more than
                # one frame behind, RESYNC to now rather than firing a burst of
                # frames to "catch up" — a burst overflows ACS's jitter buffer
                # and the caller hears choppy/broken audio.
                next_at += FRAME_DURATION_S
                delay = next_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -FRAME_DURATION_S:
                    next_at = time.monotonic()
            logger.info("Spoke %d frame(s) for %s: %s", frames, self._call_id, text[:40])
        except Exception:
            logger.exception("Speak failed for %s after %d frame(s)", self._call_id, frames)
        finally:
            self._speaking = False

    async def _synthesize_stream(self, text: str):
        """Yield raw 16kHz PCM frames for ``text`` via streaming Azure TTS.

        ``start_speaking`` returns as soon as synthesis begins; we pull
        chunks off the AudioDataStream so the first frame leaves within
        ~170ms (warm connection). Blocking SDK calls run in threads so the
        event loop (and barge-in) stay responsive. 640 bytes = 20ms @
        16kHz/16-bit mono, matching ACS's native frame size.
        """
        import asyncio

        import azure.cognitiveservices.speech as speechsdk

        if self._synth is None:
            return
        result = await asyncio.to_thread(
            lambda: self._synth.start_speaking_text_async(text).get()
        )
        stream = speechsdk.AudioDataStream(result)
        # Read in larger chunks (fewer thread hops), emit 20ms (640-byte)
        # frames matching ACS's native frame size. read_data returns a
        # variable byte count, so accumulate a carry buffer and emit only
        # whole 640-byte frames — sending an odd-sized frame makes ACS click /
        # break up the audio.
        buf = bytes(3200)
        carry = b""
        while True:
            n = await asyncio.to_thread(stream.read_data, buf)
            if n == 0:
                break
            carry += buf[:n]
            off = 0
            while len(carry) - off >= FRAME_BYTES:
                yield carry[off:off + FRAME_BYTES]
                off += FRAME_BYTES
            carry = carry[off:]
        # Flush a trailing partial frame, zero-padded (silence) to a full 20ms.
        if carry:
            yield carry + b"\x00" * (FRAME_BYTES - len(carry))
