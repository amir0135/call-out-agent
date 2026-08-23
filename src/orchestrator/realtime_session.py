"""ACS <-> Azure OpenAI Realtime (speech-to-speech) voice engine.

Alternative to the chained engine in ``media_streaming.py`` (Speech STT -> SOP
engine -> Speech TTS). Here the caller's audio is bridged straight to a GPT
realtime model that does recognition + reasoning + speech in one hop, then its
audio is bridged back to ACS. This is markedly lower latency and more natural,
but the SOP is provided as *instructions* rather than enforced by a
deterministic state machine (see the control trade-off).

Switchable at runtime via ``STREAMING_ENGINE=realtime`` (default ``chained``).

Audio: ACS streams 16 kHz PCM16; the Realtime API uses 24 kHz PCM16, so we
resample in both directions (audioop, available on the 3.11 runtime image).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .media_streaming import (
    FRAME_BYTES,
    _cached_cognitive_token,
    audio_data_out,
    extract_audio_pcm,
    parse_acs_message,
    stop_audio_out,
)
from .models import CallOutcome, CallSession

logger = logging.getLogger("orchestrator.realtime")

ACS_RATE = 16000   # ACS bidirectional media streaming PCM rate
RT_RATE = 24000    # Azure OpenAI Realtime pcm16 rate


def _config_path() -> str | None:
    """Locate config/agent_config.json (env override, container, or repo)."""
    env = os.environ.get("AGENT_CONFIG_PATH")
    if env and os.path.exists(env):
        return env
    here = Path(__file__).resolve()
    candidates = [here.parents[2], here.parents[1], Path.cwd()]
    for base in candidates:
        p = base / "config" / "agent_config.json"
        if p.exists():
            return str(p)
    return None


def load_agent_config() -> dict:
    """Load the single agent control file (config/agent_config.json), or {}."""
    path = _config_path()
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read agent config at %s; using defaults", path)
        return {}


_DEFAULT_PERSONA = (
    "You are a warm, natural-sounding phone agent for the Contoso "
    "equipment-monitoring service, calling a customer about a temperature "
    "alarm. Speak conversationally and concisely - ONE short question at a "
    "time, then WAIT for the caller to finish before responding."
)
_DEFAULT_CONTEXT = [
    "Store: {store_name}",
    "Equipment: {equipment_name}",
    "Current temperature: {current_temp} degrees (threshold {threshold_temp})",
    "Alarm time: {alarm_time}",
]
_DEFAULT_PROCEDURE = [
    "Greet them, say you're calling from the Contoso monitoring service about a "
    "high-temperature alarm at {store_name}, and briefly apologize.",
    "Ask whether they were already aware of the alarm on {equipment_name}.",
    "Ask whether anyone has looked into it yet.",
    "If not handled, recommend the equipment be inspected as soon as possible.",
    "Confirm whether they will take care of it, then thank them and say goodbye.",
]


def build_sop_instructions(session: CallSession, config: dict | None = None) -> str:
    """Render the SOP + persona from config/agent_config.json as realtime
    instructions, filling the per-call alarm fields.
    """
    cfg = config if config is not None else load_agent_config()
    rt = cfg.get("realtime", {})
    sop = cfg.get("sop", {})
    a = session.intent.alarm
    fields = {
        "store_name": a.store_name,
        "equipment_name": a.equipment_name,
        "current_temp": a.current_temp,
        "threshold_temp": a.threshold_temp,
        "alarm_time": a.alarm_time,
    }

    def _fmt(s: str) -> str:
        try:
            return s.format(**fields)
        except (KeyError, IndexError, ValueError):
            return s

    persona = rt.get("persona", _DEFAULT_PERSONA)
    language = (rt.get("language") or "auto").strip()
    lang_line = "" if language.lower() in ("", "auto") else f"\nAlways respond in {language}."
    context = "\n".join("- " + _fmt(line) for line in sop.get("context_lines", _DEFAULT_CONTEXT))
    steps = sop.get("procedure", _DEFAULT_PROCEDURE)
    procedure = "\n".join(f"{i}. {_fmt(step)}" for i, step in enumerate(steps, 1))
    escalation = _fmt(sop.get("escalation", ""))
    closing = _fmt(sop.get("closing", ""))
    return (
        f"{persona}{lang_line}\n\n"
        f"Alarm context:\n{context}\n\n"
        f"Follow this procedure, adapting naturally to the caller's answers:\n"
        f"{procedure}\n{escalation}\n{closing}"
    ).strip()


@dataclass
class RealtimeConfig:
    openai_endpoint: str                       # https://<account>.openai.azure.com/
    deployment: str = "gpt-realtime"
    api_version: str = "2025-04-01-preview"
    voice: str = "alloy"
    # Server-VAD silence before the model treats the caller's turn as finished.
    silence_ms: int = 200


WsSend = Callable[[str], Awaitable[None]]


# Strong, unambiguous farewells only. Deliberately NOT "take care" (matches the
# SOP phrase "take care of it") or bare "have a good"/"great"/"nice" (matches
# "have a good look at it"). The end_call tool is the primary hang-up signal;
# this keyword check is just a fallback for when the model says bye without it.
_GOODBYE_PHRASES = (
    "goodbye", "good-bye", "good bye", "bye for now", "bye bye", "bye-bye",
    "have a good day", "have a great day", "have a nice day",
    "have a good evening", "have a lovely day",
    "thanks for your time", "thank you for your time", "speak soon",
)


def _looks_like_goodbye(text: str) -> bool:
    t = (text or "").lower()
    # "take care" is a farewell, but "take care of it/this" is an action step.
    if "take care" in t and "take care of" not in t:
        return True
    return any(p in t for p in _GOODBYE_PHRASES)


# Phrases in the agent's goodbye that signal the call ended in escalation, not
# resolution. Used only by the goodbye FALLBACK when the model forgot to call
# end_call with an explicit outcome.
_ESCALATION_PHRASES = (
    "human operator", "a human", "transfer you", "connect you",
    "service team", "colleague", "someone from",
)

# Model-reported outcome string -> terminal CallOutcome. Keys double as the
# enum offered to the model in the end_call tool schema.
_OUTCOME_MAP = {
    "resolved": CallOutcome.RESOLVED,
    "escalate_human": CallOutcome.ESCALATE_HUMAN,
    "retry_later": CallOutcome.RETRY_LATER,
    "failed": CallOutcome.FAILED,
    "no_answer": CallOutcome.NO_ANSWER,
}

# Caller-side phrases that indicate an answering machine / voicemail picked
# up. A call that reached voicemail must be no_answer (so the retry manager
# redials) — never resolved.
_VOICEMAIL_MARKERS = (
    "voice mailbox", "voicemail", "leave a message", "leave a voice message",
    "after the tone", "after the beep", "is not available",
    "cannot take your call", "person you are trying to reach",
)


def end_call_tool() -> dict:
    """The end_call function schema shared by the realtime and voicelive
    engines. The model reports the call OUTCOME as it hangs up — this is what
    feeds retry logic, the outcome webhook and audit for the s2s engines
    (the chained engine derives its outcome from the SOP state machine)."""
    return {
        "type": "function",
        "name": "end_call",
        "description": (
            "Hang up the phone call. Call this after you have said goodbye, "
            "and ALWAYS report how the call went via the outcome argument."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": list(_OUTCOME_MAP.keys()),
                    "description": (
                        "resolved = the caller confirmed the issue is or will be "
                        "handled. escalate_human = the caller asked for a person "
                        "or could not help. retry_later = the caller asked to be "
                        "called back later. failed = the procedure could not be "
                        "completed (confusion, wrong person, refused). no_answer "
                        "= voicemail / answering machine picked up."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "One short sentence describing the result.",
                },
            },
            "required": ["outcome"],
        },
    }


END_CALL_INSTRUCTIONS = (
    "\n\nWhen the call is complete and you have said your goodbye, call the "
    "end_call function to hang up. ALWAYS pass the outcome argument: "
    "'resolved' if the caller confirmed the issue is or will be handled, "
    "'escalate_human' if they asked for a person or could not help, "
    "'retry_later' if they asked to be called back, 'failed' otherwise. "
    "If you reach voicemail or an answering machine, leave one brief message "
    "stating who you are and why you called, then call end_call with outcome "
    "'no_answer' — never 'resolved'. Do not keep talking after the goodbye."
)


class RealtimeSession:
    """Bridges one ACS media-streaming WebSocket to a GPT realtime session."""

    def __init__(
        self,
        call_id: str,
        session: CallSession,
        config: RealtimeConfig,
        ws_send: WsSend,
        on_persist: Callable[[CallSession], None] | None = None,
        on_terminal: Callable[[], Awaitable[None]] | None = None,
    ):
        self._call_id = call_id
        self._session = session
        self._cfg = config
        self._acs_send = ws_send
        self._on_persist = on_persist
        self._on_terminal = on_terminal
        self._rt = None
        self._out_buf = bytearray()      # 16 kHz PCM queued for ACS
        self._closed = False
        self._in_state = None            # resample state 16k -> 24k
        self._out_state = None           # resample state 24k -> 16k
        # The control file (config/agent_config.json) is the source of truth for
        # behaviour; env-provided RealtimeConfig values are the fallback.
        self._agent_cfg = load_agent_config()
        rt = self._agent_cfg.get("realtime", {})
        self._voice = rt.get("voice") or config.voice
        self._deployment = rt.get("model_deployment") or config.deployment
        self._silence_ms = int(rt.get("vad_silence_ms", config.silence_ms))
        self._temperature = max(0.6, min(1.2, float(rt.get("temperature", 0.8))))
        # --- Turn detection (server VAD) + interruption tuning ---------------
        # threshold: how loud a sound counts as the caller speaking (higher =
        #   ignores phone-line noise / coughs). prefix_padding: audio kept before
        #   speech onset. silence_ms: pause before the caller's turn is "done".
        self._vad_threshold = max(0.0, min(1.0, float(rt.get("vad_threshold", 0.6))))
        self._prefix_padding_ms = int(rt.get("vad_prefix_padding_ms", 300))
        # Barge-in: whether (and how readily) the caller can cut the agent off.
        #   barge_in_confirm_ms requires that many ms of *sustained* speech
        #   before the agent stops, so a cough / "mhm" doesn't clip it. 0 = stop
        #   on the first detected sound (most eager). barge_in=False = the agent
        #   always finishes its turn.
        self._barge_in = bool(rt.get("barge_in", True))
        self._barge_confirm_ms = int(rt.get("barge_in_confirm_ms", 150))
        self._barge_task: asyncio.Task | None = None
        # True while the agent is producing audio for the current turn. Barge-in
        # only applies when the agent is audible, so the caller's opening hello
        # can't clip the greeting it triggers.
        self._agent_speaking = False
        # True once the agent has greeted. In wait_for_caller mode the greeting
        # is gated on a REAL (non-empty) caller transcript so line noise / the
        # connection pop can't trigger it before the caller says hello.
        self._greeted = False
        # Turn-taking: when True the agent waits for the caller to speak first
        # (e.g. say "hello") before greeting; if the caller stays silent for
        # greeting_fallback_s the agent greets anyway so the line is never dead.
        self._wait_for_caller = bool(rt.get("wait_for_caller", True))
        self._greeting_fallback_s = float(rt.get("greeting_fallback_s", 4.0))
        # Minimum caller speech (ms) to count as a real "hello" that triggers the
        # greeting on VAD end-of-speech (skips the Whisper wait); shorter blips
        # fall through to the transcript check below.
        self._greeting_min_speech_s = float(rt.get("greeting_min_speech_ms", 250)) / 1000.0
        self._speech_started_at: float | None = None
        self._caller_spoke = False
        # Auto-hangup: when the model signals the call is done (via the end_call
        # tool or a goodbye), wait for the goodbye audio to finish playing then
        # hang up. hangup_delay_s is the tail left for ACS's jitter buffer.
        self._hangup_delay_s = float(rt.get("hangup_delay_s", 1.5))
        # After end_call, how long to wait for the model's goodbye response to
        # START generating before concluding there is none (it is normally
        # produced right after the tool call).
        self._goodbye_grace_s = float(rt.get("goodbye_grace_s", 2.5))
        self._ending = False
        # Outcome tracking: where the session outcome came from ("tool" =
        # explicit model report via end_call, "goodbye inference" = fallback).
        self._outcome_source: str | None = None
        # call_id of the end_call function call — needed to complete the tool
        # handshake if the model called it without generating a goodbye.
        self._end_call_id: str | None = None
        # Set when a caller-side transcript looks like an answering machine;
        # forces goodbye-inference to no_answer (voicemail is never resolved).
        self._voicemail_detected = False
        # Latency timing: set when the caller's turn ends (or the greeting is
        # requested); cleared when the first agent audio frame arrives so we can
        # log the true caller-stop -> agent-audio gap.
        self._resp_trigger_at: float | None = None

    def _realtime_url(self) -> str:
        host = self._cfg.openai_endpoint.split("//")[-1].rstrip("/")
        return (
            f"wss://{host}/openai/realtime"
            f"?api-version={self._cfg.api_version}&deployment={self._deployment}"
        )

    async def prewarm(self) -> None:
        """Connect + configure the model session ahead of time (during ACS
        ringing). By the time the callee answers and the media stream opens,
        the session is already live — removes the connect+configure cost
        (~0.5-1s) from the caller-perceived greeting delay. Safe no-op if
        already connected."""
        if self._rt is not None or self._closed:
            return
        import websockets

        token = await asyncio.to_thread(_cached_cognitive_token)
        self._rt = await websockets.connect(
            self._realtime_url(),
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=None,
        )
        await self._configure_session()
        logger.info("Pre-warmed model session for %s", self._call_id)

    async def close(self) -> None:
        """Close a pre-warmed session that never got a call (no-answer etc.)."""
        self._closed = True
        if self._rt is not None:
            try:
                await self._rt.close()
            except Exception:
                pass

    async def run(self, acs_ws) -> None:
        try:
            await self.prewarm()  # no-op if already pre-warmed during ringing
            rt = self._rt
            try:
                # Two turn-taking modes:
                #  - wait_for_caller (default): stay silent and let the caller
                #    say "hello" first; server VAD then auto-creates the agent's
                #    first response (the greeting). A fallback greets anyway if
                #    the caller is silent for greeting_fallback_s.
                #  - else: the agent greets immediately.
                greeter = None
                if self._wait_for_caller:
                    logger.info(
                        "Realtime session ready for %s; waiting for caller to speak first",
                        self._call_id,
                    )
                    greeter = asyncio.create_task(self._greeting_fallback())
                else:
                    logger.info("Realtime session ready for %s; greeting", self._call_id)
                    self._greeted = True
                    self._resp_trigger_at = time.monotonic()
                    await rt.send(json.dumps({"type": "response.create"}))
                sender = asyncio.create_task(self._sender_loop())
                try:
                    await asyncio.gather(
                        self._pump_acs_to_realtime(acs_ws),
                        self._pump_realtime_to_acs(),
                    )
                finally:
                    self._closed = True
                    sender.cancel()
                    if greeter is not None:
                        greeter.cancel()
                    if self._barge_task is not None:
                        self._barge_task.cancel()
            finally:
                if rt is not None:
                    try:
                        await rt.close()
                    except Exception:
                        pass
        except Exception:
            logger.exception("Realtime session failed for %s", self._call_id)

    async def _confirm_barge_in(self) -> None:
        """Halt the agent only if the caller keeps talking past the confirm
        window (ignores coughs / brief backchannels)."""
        try:
            await asyncio.sleep(self._barge_confirm_ms / 1000.0)
        except asyncio.CancelledError:
            return
        if not self._closed and self._is_agent_audible():
            await self._interrupt_agent()

    def _is_agent_audible(self) -> bool:
        """True if the agent is currently generating or still playing audio."""
        return self._agent_speaking or len(self._out_buf) >= FRAME_BYTES

    async def _wait_for_goodbye_audio(self, timeout_s: float) -> bool:
        """Wait up to timeout_s for goodbye audio to start; True if it did."""
        deadline = time.monotonic() + timeout_s
        while not self._closed and time.monotonic() < deadline:
            if self._is_agent_audible():
                return True
            await asyncio.sleep(0.02)
        return self._is_agent_audible()

    async def _interrupt_agent(self) -> None:
        """Drop the agent's queued audio and tell ACS to stop playing it."""
        self._agent_speaking = False
        self._out_buf.clear()
        try:
            await self._acs_send(stop_audio_out())
        except Exception:
            pass

    async def _finish_call(self, reason: str) -> None:
        """Hang up once, after the goodbye audio has fully played out.

        Normally the model speaks its goodbye in the same response as the
        end_call tool call. But a tool call ENDS the response — nothing
        auto-continues — so sometimes end_call arrives bare, with no goodbye
        generated at all. In that case we complete the tool handshake
        (function_call_output) and explicitly request the goodbye before
        hanging up; otherwise the caller hears silence then a click."""
        if self._ending or self._closed:
            return
        self._ending = True
        logger.info("Ending call %s (%s)", self._call_id, reason)
        started = await self._wait_for_goodbye_audio(self._goodbye_grace_s)
        if not started and self._end_call_id and self._rt is not None:
            logger.info(
                "No goodbye after end_call for %s — requesting one", self._call_id
            )
            try:
                await self._rt.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": self._end_call_id,
                        "output": '{"status": "ok - say your brief goodbye now"}',
                    },
                }))
                await self._rt.send(json.dumps({"type": "response.create"}))
                started = await self._wait_for_goodbye_audio(4.0)
            except Exception:
                logger.exception("Goodbye request failed for %s", self._call_id)
        if started:
            # Drain generation + playback fully (cap ~12s).
            deadline = time.monotonic() + 12.0
            while not self._closed and time.monotonic() < deadline:
                if not self._agent_speaking and len(self._out_buf) < FRAME_BYTES:
                    break
                await asyncio.sleep(0.02)
        # Small tail so ACS's jitter buffer drains before we drop the call.
        await asyncio.sleep(self._hangup_delay_s)
        if self._on_terminal is not None:
            try:
                await self._on_terminal()
            except Exception:
                logger.exception("on_terminal failed for %s", self._call_id)

    async def _greeting_fallback(self) -> None:
        """If the caller hasn't spoken within greeting_fallback_s, greet anyway
        so the call never starts with dead air."""
        try:
            await asyncio.sleep(self._greeting_fallback_s)
        except asyncio.CancelledError:
            return
        await self._trigger_greeting(
            "no caller speech after %.1fs" % self._greeting_fallback_s
        )

    async def _trigger_greeting(self, reason: str) -> None:
        """Send the opening greeting exactly once, then enable server-VAD
        auto-responses so the rest of the conversation flows snappily."""
        if self._greeted or self._closed or self._rt is None:
            return
        self._greeted = True
        logger.info("Greeting %s (%s)", self._call_id, reason)
        # Until now create_response was off (so noise couldn't auto-greet).
        # Turn it on for the remainder of the call.
        await self._rt.send(json.dumps({
            "type": "session.update",
            "session": {"turn_detection": self._turn_detection(create_response=True)},
        }))
        self._resp_trigger_at = time.monotonic()
        await self._rt.send(json.dumps({"type": "response.create"}))

    def _turn_detection(self, create_response: bool) -> dict:
        return {
            "type": "server_vad",
            "threshold": self._vad_threshold,
            "prefix_padding_ms": self._prefix_padding_ms,
            "silence_duration_ms": self._silence_ms,
            "create_response": create_response,
        }

    async def _configure_session(self) -> None:
        # In wait_for_caller mode start with auto-response OFF so a noise blip
        # can't fire the greeting; _trigger_greeting flips it on once the caller
        # actually speaks (or the silence fallback fires).
        auto_response = not self._wait_for_caller
        instructions = (
            build_sop_instructions(self._session, self._agent_cfg)
            + END_CALL_INSTRUCTIONS
        )
        await self._rt.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "instructions": instructions,
                "voice": self._voice,
                "temperature": self._temperature,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": self._turn_detection(create_response=auto_response),
                "input_audio_transcription": {"model": "whisper-1"},
                "tools": [end_call_tool()],
                "tool_choice": "auto",
            },
        }))

    # --- Outcome tracking (s2s engines) --------------------------------------
    def _record_outcome(self, outcome_str: str, summary: str, source: str) -> None:
        """Set the session's terminal outcome (drives retry logic, the outcome
        webhook and audit). Explicit model reports win over inference; the
        first explicit report wins over later ones."""
        # A voicemail pickup can never be 'resolved' — nobody confirmed
        # anything. Downgrade even explicit model reports.
        if self._voicemail_detected and outcome_str == "resolved":
            outcome_str, summary = "no_answer", summary or "Reached voicemail."
        outcome = _OUTCOME_MAP.get((outcome_str or "").strip().lower())
        if outcome is None:
            return
        if self._outcome_source == "tool" and source != "tool":
            return
        # Dedupe: the realtime API fires both function_call_arguments.done and
        # output_item.done for one tool call — record (and log) only once.
        if self._session.outcome == outcome and self._outcome_source == source:
            return
        self._outcome_source = source
        self._session.outcome = outcome
        if summary:
            self._session.metadata["outcome_summary"] = summary[:300]
        logger.info(
            "Outcome for %s: %s (%s)%s",
            self._call_id, outcome.value, source,
            f" — {summary[:80]}" if summary else "",
        )
        if self._on_persist is not None:
            try:
                self._on_persist(self._session)
            except Exception:
                logger.exception("Persisting outcome failed for %s", self._call_id)

    def _infer_outcome_from_goodbye(self, transcript: str) -> None:
        """Fallback when the model says goodbye without calling end_call:
        voicemail always means no_answer; an escalation phrasing means
        escalate_human; otherwise a completed goodbye counts as resolved."""
        if self._session.outcome is not None:
            return
        if self._voicemail_detected:
            self._record_outcome("no_answer", "Reached voicemail.", "goodbye inference")
            return
        t = (transcript or "").lower()
        if any(p in t for p in _ESCALATION_PHRASES):
            self._record_outcome("escalate_human", "", "goodbye inference")
        else:
            self._record_outcome("resolved", "", "goodbye inference")

    # --- ACS caller audio -> Realtime ---
    async def _pump_acs_to_realtime(self, acs_ws) -> None:
        import audioop

        async for raw in acs_ws.iter_text():
            if self._closed:
                break
            try:
                kind, m = parse_acs_message(raw)
                if kind == "AudioData" and self._rt is not None:
                    pcm = extract_audio_pcm(m)
                    if pcm:
                        up, self._in_state = audioop.ratecv(
                            pcm, 2, 1, ACS_RATE, RT_RATE, self._in_state
                        )
                        await self._rt.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(up).decode("ascii"),
                        }))
            except Exception:
                logger.exception("ACS->RT error for %s", self._call_id)

    # --- Realtime model audio/events -> ACS ---
    async def _pump_realtime_to_acs(self) -> None:
        import audioop

        async for raw in self._rt:
            if self._closed:
                break
            try:
                evt = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            t = evt.get("type", "")
            if t == "response.audio.delta":
                if self._resp_trigger_at is not None:
                    gap_ms = (time.monotonic() - self._resp_trigger_at) * 1000.0
                    logger.info("RT first-audio [%s]: %.0f ms", self._call_id, gap_ms)
                    self._resp_trigger_at = None
                self._agent_speaking = True
                b64 = evt.get("delta", "")
                if b64:
                    pcm24 = base64.b64decode(b64)
                    down, self._out_state = audioop.ratecv(
                        pcm24, 2, 1, RT_RATE, ACS_RATE, self._out_state
                    )
                    self._out_buf.extend(down)
            elif t in ("response.done", "response.audio.done"):
                # Agent finished generating this turn.
                self._agent_speaking = False
            elif t == "input_audio_buffer.speech_started":
                # Caller started talking. Confirmed barge-in: only halt the agent
                # after barge_in_confirm_ms of sustained speech so a brief cough
                # or backchannel ("mhm") doesn't clip it. Crucially, only when the
                # agent is ACTUALLY speaking — otherwise the caller's opening
                # "hello" would fire a StopAudio that clips the start of the
                # greeting it just triggered.
                self._caller_spoke = True
                self._speech_started_at = time.monotonic()
                if self._barge_in and self._is_agent_audible():
                    if self._barge_confirm_ms <= 0:
                        await self._interrupt_agent()
                    elif self._barge_task is None or self._barge_task.done():
                        self._barge_task = asyncio.create_task(self._confirm_barge_in())
            elif t == "input_audio_buffer.speech_stopped":
                # If speech stopped before the confirm window, it was a blip —
                # cancel the pending barge-in so the agent keeps talking.
                if self._barge_task is not None and not self._barge_task.done():
                    self._barge_task.cancel()
                    self._barge_task = None
                # Greet as soon as the caller's opening turn ends — gated by
                # speech DURATION (a real "hello" is long enough; a noise blip
                # isn't) so we don't pay the ~0.3-0.6s Whisper-transcription wait
                # before the greeting. The transcript path below is a backup.
                if not self._greeted and self._wait_for_caller:
                    dur = time.monotonic() - (self._speech_started_at or 0.0)
                    if self._speech_started_at and dur >= self._greeting_min_speech_s:
                        await self._trigger_greeting("caller spoke")
                else:
                    # Normal turn — start the response timer (auto-response).
                    self._resp_trigger_at = time.monotonic()
            elif t == "response.audio_transcript.done":
                transcript = (evt.get("transcript") or "")
                logger.info("RT agent [%s]: %s", self._call_id, transcript[:90])
                # Fallback to the tool: if the model said goodbye but didn't call
                # end_call, infer the outcome and still hang up once the goodbye
                # has played.
                if self._greeted and _looks_like_goodbye(transcript):
                    self._infer_outcome_from_goodbye(transcript)
                    asyncio.create_task(self._finish_call("goodbye detected"))
            elif t in ("response.function_call_arguments.done", "response.output_item.done"):
                item = evt.get("item", {}) or {}
                name = evt.get("name") or item.get("name")
                if name == "end_call":
                    self._end_call_id = (
                        evt.get("call_id") or item.get("call_id") or self._end_call_id
                    )
                    raw_args = evt.get("arguments") or item.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    self._record_outcome(
                        args.get("outcome", ""), args.get("summary", ""), "tool"
                    )
                    asyncio.create_task(self._finish_call("end_call tool"))
            elif t == "conversation.item.input_audio_transcription.completed":
                txt = (evt.get("transcript") or "").strip()
                logger.info("RT caller [%s]: %s", self._call_id, txt[:90])
                low = txt.lower()
                if any(m in low for m in _VOICEMAIL_MARKERS):
                    self._voicemail_detected = True
                    logger.info("Voicemail detected for %s", self._call_id)
                # Real words from the caller — greet now (ignores empty/noise).
                if txt and not self._greeted and self._wait_for_caller:
                    await self._trigger_greeting("caller spoke")
            elif t == "error":
                logger.warning("RT error [%s]: %s", self._call_id, str(evt.get("error"))[:200])

    # --- Paced sender: emit 20ms frames to ACS so we never overrun its buffer ---
    async def _sender_loop(self) -> None:
        next_at = time.monotonic()
        while not self._closed:
            if len(self._out_buf) >= FRAME_BYTES:
                frame = bytes(self._out_buf[:FRAME_BYTES])
                del self._out_buf[:FRAME_BYTES]
                try:
                    await self._acs_send(audio_data_out(frame))
                except Exception:
                    break
                next_at += 0.020
                delay = next_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -0.020:
                    next_at = time.monotonic()
            else:
                next_at = time.monotonic()
                await asyncio.sleep(0.005)
