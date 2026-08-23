"""Tests for the Voice Live engine (voicelive_session.py).

The engine subclasses RealtimeSession, so conversation behaviour (greeting,
barge-in, hangup) is covered by the shared logic; here we validate what the
subclass overrides: the endpoint URL, the session.update payload (Azure voice,
semantic VAD, audio cleanup, 16 kHz input) and the no-resample input pump.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.models import AlarmContext, CallIntent, CallSession
from orchestrator.voicelive_session import VoiceLiveConfig, VoiceLiveSession


@pytest.fixture
def session() -> CallSession:
    alarm = AlarmContext(
        alarm_id="ALM-1", alarm_type="high_temperature", store_name="Test Store",
        store_id="ST-1", equipment_name="Cooler A", current_temp=14.0,
        threshold_temp=8.0, alarm_time="2026-06-17T10:00:00Z", customer_id="CUST-1",
    )
    intent = CallIntent(intent_id="INT-1", alarm=alarm, phone_number="+4512345678")
    return CallSession(call_id="CALL-1", intent=intent)


class FakeRt:
    """Captures JSON messages sent to the (fake) Voice Live WebSocket."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class FakeAcsWs:
    """Yields canned ACS media-streaming text frames."""

    def __init__(self, frames: list[str]):
        self._frames = frames

    async def iter_text(self):
        for f in self._frames:
            yield f


async def _noop_send(_: str) -> None:
    return None


def make_session(session: CallSession, monkeypatch, agent_cfg: dict | None = None,
                 **cfg_overrides) -> VoiceLiveSession:
    # Isolate from the repo's config/agent_config.json.
    import orchestrator.realtime_session as rt_mod
    monkeypatch.setattr(rt_mod, "load_agent_config", lambda: agent_cfg or {})
    config = VoiceLiveConfig(
        endpoint="https://my-foundry.cognitiveservices.azure.com/",
        **cfg_overrides,
    )
    return VoiceLiveSession(
        call_id="CALL-1", session=session, config=config, ws_send=_noop_send,
    )


class TestUrl:
    def test_voice_live_url(self, session, monkeypatch):
        vl = make_session(session, monkeypatch)
        url = vl._realtime_url()
        assert url == (
            "wss://my-foundry.cognitiveservices.azure.com/voice-live/realtime"
            "?api-version=2026-04-10&model=gpt-realtime"
        )

    def test_model_from_config_file(self, session, monkeypatch):
        vl = make_session(
            session, monkeypatch,
            agent_cfg={"voicelive": {"model": "gpt-realtime-mini"}},
        )
        assert "model=gpt-realtime-mini" in vl._realtime_url()

    def test_byom_profile_in_url(self, session, monkeypatch):
        vl = make_session(
            session, monkeypatch,
            agent_cfg={"voicelive": {
                "model": "gpt-realtime-2.1-mini",
                "byom_profile": "byom-azure-openai-realtime",
            }},
        )
        url = vl._realtime_url()
        assert "model=gpt-realtime-2.1-mini" in url
        assert "&profile=byom-azure-openai-realtime" in url

    def test_no_byom_no_profile_param(self, session, monkeypatch):
        vl = make_session(session, monkeypatch)
        assert "profile=" not in vl._realtime_url()


class TestSessionConfig:
    def _configure(self, vl: VoiceLiveSession) -> dict:
        vl._rt = FakeRt()
        asyncio.run(vl._configure_session())
        assert vl._rt.sent[0]["type"] == "session.update"
        return vl._rt.sent[0]["session"]

    def test_azure_voice_object(self, session, monkeypatch):
        vl = make_session(session, monkeypatch)
        s = self._configure(vl)
        assert s["voice"] == {
            "name": "en-GB-OllieMultilingualNeural",
            "type": "azure-standard",
        }

    def test_openai_voice_stays_string(self, session, monkeypatch):
        vl = make_session(
            session, monkeypatch, voice_name="alloy", voice_type="openai",
        )
        s = self._configure(vl)
        assert s["voice"] == "alloy"

    def test_voice_rate_included(self, session, monkeypatch):
        vl = make_session(
            session, monkeypatch,
            agent_cfg={"voicelive": {"voice_rate": "1.1"}},
        )
        s = self._configure(vl)
        assert s["voice"]["rate"] == "1.1"

    def test_semantic_vad_and_cleanup(self, session, monkeypatch):
        vl = make_session(session, monkeypatch)
        s = self._configure(vl)
        td = s["turn_detection"]
        assert td["type"] == "azure_semantic_vad"
        assert td["remove_filler_words"] is True
        assert s["input_audio_sampling_rate"] == 16000
        assert s["input_audio_noise_reduction"] == {
            "type": "azure_deep_noise_suppression"
        }
        assert s["input_audio_echo_cancellation"] == {
            "type": "server_echo_cancellation"
        }

    def test_server_vad_has_no_filler_key(self, session, monkeypatch):
        vl = make_session(
            session, monkeypatch,
            agent_cfg={"voicelive": {"vad_type": "server_vad"}},
        )
        s = self._configure(vl)
        assert "remove_filler_words" not in s["turn_detection"]

    def test_cleanup_can_be_disabled(self, session, monkeypatch):
        vl = make_session(
            session, monkeypatch,
            agent_cfg={"voicelive": {
                "noise_suppression": False, "echo_cancellation": False,
            }},
        )
        s = self._configure(vl)
        assert "input_audio_noise_reduction" not in s
        assert "input_audio_echo_cancellation" not in s

    def test_vad_knobs_from_config(self, session, monkeypatch):
        vl = make_session(
            session, monkeypatch,
            agent_cfg={"voicelive": {
                "vad_silence_ms": 250, "vad_threshold": 0.7,
                "vad_prefix_padding_ms": 200,
            }},
        )
        s = self._configure(vl)
        td = s["turn_detection"]
        assert td["silence_duration_ms"] == 250
        assert td["threshold"] == 0.7
        assert td["prefix_padding_ms"] == 200

    def test_sop_instructions_and_end_call_tool(self, session, monkeypatch):
        vl = make_session(session, monkeypatch)
        s = self._configure(vl)
        assert "Test Store" in s["instructions"]
        assert "end_call" in s["instructions"]
        assert s["tools"][0]["name"] == "end_call"


class TestInputPump:
    def test_audio_forwarded_without_resample(self, session, monkeypatch):
        vl = make_session(session, monkeypatch)
        rt = FakeRt()
        vl._rt = rt
        pcm = bytes(range(64)) * 10  # 640 bytes = one 20ms 16kHz frame
        frame = json.dumps({
            "kind": "AudioData",
            "audioData": {"data": base64.b64encode(pcm).decode("ascii")},
        })
        asyncio.run(vl._pump_acs_to_realtime(FakeAcsWs([frame])))
        assert len(rt.sent) == 1
        msg = rt.sent[0]
        assert msg["type"] == "input_audio_buffer.append"
        # Byte-for-byte identical: 16 kHz passes through with no resampling.
        assert base64.b64decode(msg["audio"]) == pcm

    def test_non_audio_frames_ignored(self, session, monkeypatch):
        vl = make_session(session, monkeypatch)
        rt = FakeRt()
        vl._rt = rt
        meta = json.dumps({"kind": "AudioMetadata", "audioMetadata": {}})
        asyncio.run(vl._pump_acs_to_realtime(FakeAcsWs([meta])))
        assert rt.sent == []


class TestVoiceLiveEnhancements:
    """The Voice Live-only conversational parameters."""

    def _configure(self, vl: VoiceLiveSession) -> dict:
        vl._rt = FakeRt()
        asyncio.run(vl._configure_session())
        return vl._rt.sent[0]["session"]

    def test_turn_detection_full_surface(self, session, monkeypatch):
        vl = make_session(session, monkeypatch, agent_cfg={"voicelive": {
            "languages": ["en"], "vad_speech_duration_ms": 100,
        }})
        td = self._configure(vl)["turn_detection"]
        assert td["speech_duration_ms"] == 100
        assert td["interrupt_response"] is True
        assert td["auto_truncate"] is True
        assert td["languages"] == ["en"]

    def test_transcription_domain_prompt(self, session, monkeypatch):
        vl = make_session(session, monkeypatch, agent_cfg={"voicelive": {
            "languages": ["en"],
        }})
        tc = self._configure(vl)["input_audio_transcription"]
        assert tc["model"] == "gpt-4o-transcribe"
        assert tc["language"] == "en"
        assert "Test Store" in tc["prompt"] and "Cooler A" in tc["prompt"]

    def test_whisper_has_no_prompt(self, session, monkeypatch):
        vl = make_session(session, monkeypatch, agent_cfg={"voicelive": {
            "transcription_model": "whisper-1",
        }})
        tc = self._configure(vl)["input_audio_transcription"]
        assert tc["model"] == "whisper-1"
        assert "prompt" not in tc


class TestOutcomeTracking:
    """Model-reported outcomes via the end_call tool (shared s2s logic)."""

    def test_end_call_tool_schema(self):
        from orchestrator.realtime_session import end_call_tool

        tool = end_call_tool()
        assert tool["name"] == "end_call"
        props = tool["parameters"]["properties"]
        assert set(props["outcome"]["enum"]) == {
            "resolved", "escalate_human", "retry_later", "failed", "no_answer",
        }
        assert tool["parameters"]["required"] == ["outcome"]

    def test_tool_outcome_recorded(self, session, monkeypatch):
        from orchestrator.models import CallOutcome

        vl = make_session(session, monkeypatch)
        persisted = []
        vl._on_persist = persisted.append
        vl._record_outcome("resolved", "Caller will inspect the cooler.", "tool")
        assert session.outcome == CallOutcome.RESOLVED
        assert session.metadata["outcome_summary"] == "Caller will inspect the cooler."
        assert persisted == [session]

    def test_unknown_outcome_ignored(self, session, monkeypatch):
        vl = make_session(session, monkeypatch)
        vl._record_outcome("banana", "", "tool")
        assert session.outcome is None

    def test_tool_wins_over_inference(self, session, monkeypatch):
        from orchestrator.models import CallOutcome

        vl = make_session(session, monkeypatch)
        vl._record_outcome("escalate_human", "", "tool")
        vl._infer_outcome_from_goodbye("thanks, have a good day")
        assert session.outcome == CallOutcome.ESCALATE_HUMAN

    def test_duplicate_tool_events_recorded_once(self, session, monkeypatch):
        """The realtime API fires two events per tool call — persist once."""
        vl = make_session(session, monkeypatch)
        persisted = []
        vl._on_persist = persisted.append
        vl._record_outcome("resolved", "ok", "tool")
        vl._record_outcome("resolved", "ok", "tool")
        assert len(persisted) == 1


class TestFinishCall:
    """Bare end_call (no goodbye generated) must trigger the tool handshake."""

    def _run_finish(self, vl):
        vl._hangup_delay_s = 0.0
        vl._goodbye_grace_s = 0.05
        asyncio.run(vl._finish_call("test"))

    def test_bare_end_call_requests_goodbye(self, session, monkeypatch):
        vl = make_session(session, monkeypatch)
        vl._rt = FakeRt()
        vl._end_call_id = "call_abc"
        done = []
        async def on_terminal(): done.append(True)
        vl._on_terminal = on_terminal
        self._run_finish(vl)
        types = [m["type"] for m in vl._rt.sent]
        assert "conversation.item.create" in types
        assert "response.create" in types
        out_item = next(m for m in vl._rt.sent if m["type"] == "conversation.item.create")
        assert out_item["item"]["call_id"] == "call_abc"
        assert done == [True]

    def test_goodbye_already_playing_no_handshake(self, session, monkeypatch):
        vl = make_session(session, monkeypatch)
        vl._rt = FakeRt()
        vl._end_call_id = "call_abc"
        vl._agent_speaking = True  # goodbye already generating

        async def stop_speaking():
            await asyncio.sleep(0.1)
            vl._agent_speaking = False

        async def run():
            vl._hangup_delay_s = 0.0
            vl._goodbye_grace_s = 0.05
            task = asyncio.create_task(stop_speaking())
            await vl._finish_call("test")
            await task

        asyncio.run(run())
        assert [m["type"] for m in vl._rt.sent] == []


class TestPrewarm:
    """Pre-warm during ringing: connect + configure once, close if unclaimed."""

    def test_prewarm_connects_and_configures(self, session, monkeypatch):
        import orchestrator.realtime_session as rt_mod

        rt = FakeRt()
        rt.close_called = False
        async def fake_close(): rt.close_called = True
        rt.close = fake_close

        async def fake_connect(url, **kwargs):
            fake_connect.url = url
            return rt

        monkeypatch.setattr(rt_mod, "_cached_cognitive_token", lambda: "tok")
        import websockets
        monkeypatch.setattr(websockets, "connect", fake_connect)

        vl = make_session(session, monkeypatch)
        asyncio.run(vl.prewarm())
        assert vl._rt is rt
        assert vl._rt.sent[0]["type"] == "session.update"
        assert "/voice-live/realtime" in fake_connect.url

        # Second prewarm is a no-op (no duplicate session.update).
        n = len(rt.sent)
        asyncio.run(vl.prewarm())
        assert len(rt.sent) == n

        # Unclaimed sessions can be closed.
        asyncio.run(vl.close())
        assert rt.close_called and vl._closed

    def test_goodbye_inference_resolved(self, session, monkeypatch):
        from orchestrator.models import CallOutcome

        vl = make_session(session, monkeypatch)
        vl._infer_outcome_from_goodbye("Thank you, goodbye and have a good day.")
        assert session.outcome == CallOutcome.RESOLVED

    def test_goodbye_inference_escalation(self, session, monkeypatch):
        from orchestrator.models import CallOutcome

        vl = make_session(session, monkeypatch)
        vl._infer_outcome_from_goodbye(
            "I will connect you to a human operator from the service team. Goodbye."
        )
        assert session.outcome == CallOutcome.ESCALATE_HUMAN

    def test_instructions_mention_outcome(self, session, monkeypatch):
        vl = make_session(session, monkeypatch)
        vl._rt = FakeRt()
        asyncio.run(vl._configure_session())
        instructions = vl._rt.sent[0]["session"]["instructions"]
        assert "outcome" in instructions and "resolved" in instructions
        assert "voicemail" in instructions

    def test_voicemail_forces_no_answer_on_inference(self, session, monkeypatch):
        from orchestrator.models import CallOutcome

        vl = make_session(session, monkeypatch)
        vl._voicemail_detected = True
        vl._infer_outcome_from_goodbye("I'll leave a message. Goodbye.")
        assert session.outcome == CallOutcome.NO_ANSWER

    def test_voicemail_downgrades_tool_resolved(self, session, monkeypatch):
        from orchestrator.models import CallOutcome

        vl = make_session(session, monkeypatch)
        vl._voicemail_detected = True
        vl._record_outcome("resolved", "", "tool")
        assert session.outcome == CallOutcome.NO_ANSWER

    def test_voicemail_does_not_touch_explicit_no_answer(self, session, monkeypatch):
        from orchestrator.models import CallOutcome

        vl = make_session(session, monkeypatch)
        vl._voicemail_detected = True
        vl._record_outcome("no_answer", "Voicemail, left a message.", "tool")
        assert session.outcome == CallOutcome.NO_ANSWER
        assert session.metadata["outcome_summary"] == "Voicemail, left a message."
