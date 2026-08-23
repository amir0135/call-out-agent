"""ACS <-> Azure Voice Live API voice engine (managed speech-to-speech).

Third voice engine next to ``chained`` (media_streaming.py) and ``realtime``
(realtime_session.py). Voice Live wraps the same gpt-realtime brain in a
managed audio front-end: Azure semantic VAD, deep noise suppression, server
echo cancellation, and — the big one — Azure neural / custom voices on top of
a speech-to-speech model.

The Voice Live API is event-compatible with the Azure OpenAI Realtime API, so
this engine subclasses :class:`RealtimeSession` and overrides only what
differs:

* WebSocket URL: ``wss://<resource>/voice-live/realtime?api-version=...&model=...``
  (a Foundry / AI Services or Speech resource endpoint, NOT the OpenAI one).
* ``session.update`` payload: Azure voice object, ``azure_semantic_vad``,
  noise suppression, echo cancellation, ``input_audio_sampling_rate: 16000``.
* Input audio: ACS PCM16@16k is forwarded as-is (no 16k->24k resample) because
  Voice Live accepts 16 kHz input natively. Output stays 24 kHz -> 16 kHz via
  the inherited downsample path.

Switchable at runtime via ``STREAMING_ENGINE=voicelive``.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .media_streaming import extract_audio_pcm, parse_acs_message
from .models import CallSession
from .realtime_session import (
    ACS_RATE,
    END_CALL_INSTRUCTIONS,
    RealtimeConfig,
    RealtimeSession,
    build_sop_instructions,
    end_call_tool,
)

logger = logging.getLogger("orchestrator.voicelive")

DEFAULT_API_VERSION = "2026-04-10"

WsSend = Callable[[str], Awaitable[None]]


@dataclass
class VoiceLiveConfig:
    """Env-provided defaults; config/agent_config.json 'voicelive' overrides."""

    endpoint: str                     # https://<resource>.cognitiveservices.azure.com/
    model: str = "gpt-realtime"       # managed model — no deployment needed
    api_version: str = DEFAULT_API_VERSION
    voice_name: str = "en-GB-OllieMultilingualNeural"
    voice_type: str = "azure-standard"  # azure-standard | azure-custom | openai
    voice_rate: str = ""              # "0.5".."1.5", empty = default


class VoiceLiveSession(RealtimeSession):
    """Bridges one ACS media-streaming WebSocket to a Voice Live session.

    Conversation behaviour (wait-for-caller greeting, confirmed barge-in,
    end_call auto-hangup, paced 20 ms sender) is inherited from
    :class:`RealtimeSession`; the persona/SOP knobs keep coming from the
    shared ``realtime`` section of config/agent_config.json so the two
    engines stay behaviourally identical. The ``voicelive`` section holds
    only what is specific to this engine (voice, VAD type, audio cleanup).
    """

    def __init__(
        self,
        call_id: str,
        session: CallSession,
        config: VoiceLiveConfig,
        ws_send: WsSend,
        on_persist: Callable[[CallSession], None] | None = None,
        on_terminal: Callable[[], Awaitable[None]] | None = None,
    ):
        super().__init__(
            call_id=call_id,
            session=session,
            config=RealtimeConfig(
                openai_endpoint=config.endpoint,
                deployment=config.model,
                api_version=config.api_version,
            ),
            ws_send=ws_send,
            on_persist=on_persist,
            on_terminal=on_terminal,
        )
        vl = self._agent_cfg.get("voicelive", {})
        self._model = vl.get("model") or config.model
        # BYOM: when set (e.g. byom-azure-openai-realtime), 'model' is the name
        # of OUR deployment on the Foundry resource instead of a Voice Live
        # managed model — lets us run models the managed catalog doesn't have
        # yet (gpt-realtime-2.1-mini).
        self._byom_profile = vl.get("byom_profile", "")
        self._voice_name = vl.get("voice_name") or config.voice_name
        self._voice_type = vl.get("voice_type") or config.voice_type
        self._voice_rate = str(vl.get("voice_rate", config.voice_rate) or "")
        # Azure HD voices accept a temperature of their own; optional.
        self._voice_temperature = vl.get("voice_temperature")
        self._vad_type = vl.get("vad_type", "azure_semantic_vad")
        self._remove_filler_words = bool(vl.get("remove_filler_words", True))
        self._noise_suppression = bool(vl.get("noise_suppression", True))
        self._echo_cancellation = bool(vl.get("echo_cancellation", True))
        # Semantic VAD understands end-of-sentence; it can run with a shorter
        # silence window than plain server_vad without cutting people off.
        if "vad_silence_ms" in vl:
            self._silence_ms = int(vl["vad_silence_ms"])
        if "vad_threshold" in vl:
            self._vad_threshold = max(0.0, min(1.0, float(vl["vad_threshold"])))
        if "vad_prefix_padding_ms" in vl:
            self._prefix_padding_ms = int(vl["vad_prefix_padding_ms"])
        # --- Voice Live-only conversational enhancements ---------------------
        # Min caller speech (ms) before VAD treats it as speech at all (default
        # 80 for azure_semantic_vad) — raises robustness against pops/clicks.
        self._speech_duration_ms = int(vl.get("vad_speech_duration_ms", 80))
        # Language hint(s): sharpen filler-word removal + transcription.
        self._languages = vl.get("languages") or []
        # Server-side barge-in handling: interrupt the response when the caller
        # talks over it, and truncate the conversation context to what was
        # actually heard (so the model doesn't believe it said the cut part).
        self._interrupt_response = bool(vl.get("interrupt_response", True))
        self._auto_truncate = bool(vl.get("auto_truncate", True))
        # Input transcription: gpt-4o-transcribe beats whisper-1 and accepts a
        # domain prompt; we seed it with the alarm's store/equipment names.
        self._transcription_model = vl.get("transcription_model", "gpt-4o-transcribe")

    # --- Voice Live endpoint ------------------------------------------------
    def _realtime_url(self) -> str:
        host = self._cfg.openai_endpoint.split("//")[-1].rstrip("/")
        url = (
            f"wss://{host}/voice-live/realtime"
            f"?api-version={self._cfg.api_version}&model={self._model}"
        )
        if self._byom_profile:
            url += f"&profile={self._byom_profile}"
        return url

    # --- Session configuration ----------------------------------------------
    def _voice_config(self) -> dict | str:
        """Azure voices are a structured object; OpenAI voices stay a string."""
        if self._voice_type == "openai":
            return self._voice_name
        voice: dict = {"name": self._voice_name, "type": self._voice_type}
        if self._voice_rate:
            voice["rate"] = self._voice_rate
        if self._voice_temperature is not None:
            voice["temperature"] = float(self._voice_temperature)
        return voice

    def _turn_detection(self, create_response: bool) -> dict:
        td = {
            "type": self._vad_type,
            "threshold": self._vad_threshold,
            "prefix_padding_ms": self._prefix_padding_ms,
            "speech_duration_ms": self._speech_duration_ms,
            "silence_duration_ms": self._silence_ms,
            "create_response": create_response,
        }
        if self._vad_type.startswith("azure_semantic_vad"):
            td["remove_filler_words"] = self._remove_filler_words
            td["interrupt_response"] = self._interrupt_response
            td["auto_truncate"] = self._auto_truncate
            if self._languages:
                td["languages"] = list(self._languages)
        return td

    def _transcription_config(self) -> dict:
        cfg: dict = {"model": self._transcription_model}
        if self._languages:
            cfg["language"] = self._languages[0]
        if self._transcription_model.startswith("gpt-4o"):
            # Domain terms from the alarm sharpen recognition of names the
            # caller will say back ("yes, Cooler 3 at the Central Market...").
            a = self._session.intent.alarm
            cfg["prompt"] = (
                f"Expected terminology: Contoso, {a.store_name}, "
                f"{a.equipment_name}, temperature alarm."
            )
        return cfg

    async def _configure_session(self) -> None:
        auto_response = not self._wait_for_caller
        instructions = (
            build_sop_instructions(self._session, self._agent_cfg)
            + END_CALL_INSTRUCTIONS
        )
        session: dict = {
            "modalities": ["audio", "text"],
            "instructions": instructions,
            "voice": self._voice_config(),
            "temperature": self._temperature,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            # ACS streams 16 kHz — Voice Live accepts it natively, so the
            # caller's audio is forwarded without resampling (see the pump).
            "input_audio_sampling_rate": ACS_RATE,
            "turn_detection": self._turn_detection(create_response=auto_response),
            "input_audio_transcription": self._transcription_config(),
            "tools": [end_call_tool()],
            "tool_choice": "auto",
        }
        if self._noise_suppression:
            session["input_audio_noise_reduction"] = {
                "type": "azure_deep_noise_suppression"
            }
        if self._echo_cancellation:
            session["input_audio_echo_cancellation"] = {
                "type": "server_echo_cancellation"
            }
        await self._rt.send(json.dumps({"type": "session.update", "session": session}))

    # --- ACS caller audio -> Voice Live (no resample: 16 kHz in, 16 kHz out) --
    async def _pump_acs_to_realtime(self, acs_ws) -> None:
        async for raw in acs_ws.iter_text():
            if self._closed:
                break
            try:
                kind, m = parse_acs_message(raw)
                if kind == "AudioData" and self._rt is not None:
                    pcm = extract_audio_pcm(m)
                    if pcm:
                        await self._rt.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm).decode("ascii"),
                        }))
            except Exception:
                logger.exception("ACS->VL error for %s", self._call_id)
