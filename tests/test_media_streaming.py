"""Tests for the media-streaming worker — wire protocol + SOP driver.

The live Azure Speech loop (MediaStreamingSession) needs the Speech SDK and
a real call to validate; here we cover the pure, deterministic layers.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.media_streaming import (
    StreamingConversation,
    StreamTurn,
    audio_data_out,
    extract_audio_pcm,
    is_greeting_only,
    parse_acs_message,
    stop_audio_out,
)
from orchestrator.models import (
    AgentResponse,
    AlarmContext,
    CallIntent,
    CallSession,
)
from orchestrator.sop_engine import SOPEngine


# --- Wire protocol --------------------------------------------------------
class TestProtocol:
    def test_parse_audio_data(self):
        raw = json.dumps({"kind": "AudioData", "audioData": {"data": "AAA="}})
        kind, msg = parse_acs_message(raw)
        assert kind == "AudioData"
        assert msg["audioData"]["data"] == "AAA="

    def test_parse_garbage_is_unknown(self):
        kind, msg = parse_acs_message("not json")
        assert kind == "Unknown"
        assert msg == {}

    def test_extract_pcm_decodes_base64(self):
        pcm = b"\x01\x02\x03\x04"
        msg = {"audioData": {"data": base64.b64encode(pcm).decode()}}
        assert extract_audio_pcm(msg) == pcm

    def test_extract_pcm_skips_silent_frames(self):
        msg = {"audioData": {"data": "AAA=", "silent": True}}
        assert extract_audio_pcm(msg) is None

    def test_audio_data_out_roundtrip(self):
        pcm = b"\x10\x20\x30"
        out = json.loads(audio_data_out(pcm))
        # Outbound uses PascalCase (ACS plays it only in this exact shape).
        assert out["Kind"] == "AudioData"
        assert base64.b64decode(out["AudioData"]["Data"]) == pcm
        assert out["StopAudio"] is None

    def test_greeting_only_filter(self):
        # Pickup greetings are ignored; real answers are not.
        assert is_greeting_only("Hello?")
        assert is_greeting_only("hi")
        assert is_greeting_only("Hej hej")
        assert not is_greeting_only("yes")
        assert not is_greeting_only("no")
        assert not is_greeting_only("hello yes I am aware")
        assert not is_greeting_only("")

    def test_stop_audio_out(self):
        out = json.loads(stop_audio_out())
        assert out["Kind"] == "StopAudio"
        assert out["StopAudio"] == {}


# --- SOP conversation driver ---------------------------------------------
@pytest.fixture
def session() -> CallSession:
    alarm = AlarmContext(
        alarm_id="ALM-1", alarm_type="high_temperature", store_name="Test Store",
        store_id="ST-1", equipment_name="Cooler A", current_temp=14.0,
        threshold_temp=8.0, alarm_time="2026-06-17T10:00:00Z", customer_id="CUST-1",
    )
    intent = CallIntent(intent_id="INT-1", alarm=alarm, phone_number="+4512345678")
    return CallSession(call_id="CALL-1", intent=intent)


@pytest.fixture
def sop_engine() -> SOPEngine:
    eng = SOPEngine()
    eng.load()
    return eng


class TestStreamingConversation:
    @pytest.mark.asyncio
    async def test_opening_chains_greeting_into_first_question(self, sop_engine, session):
        convo = StreamingConversation(sop_engine, session, reason_fn=None)
        turns = convo.opening_turns()
        # Greeting (speak) must auto-advance into the first ask step, so the
        # agent says at least two things and doesn't go silent.
        assert len(turns) >= 2
        assert "monitoring" in turns[0].lower()
        assert "?" in turns[-1]  # ends on a question

    @pytest.mark.asyncio
    async def test_respond_advances_sop(self, sop_engine, session):
        # Move past greeting to the first ask step.
        session.current_step = "ask_awareness"

        async def fake_reason(_request):
            return AgentResponse(
                next_step="ask_action_taken",
                utterance="(ignored; rendered from SOP)",
                reasoning="matched yes",
            )

        convo = StreamingConversation(sop_engine, session, reason_fn=fake_reason)
        turn = await convo.respond_to("yes")
        assert isinstance(turn, StreamTurn)
        assert turn.is_terminal is False
        assert len(turn.utterances) >= 1
        # Caller + agent turns recorded.
        assert session.conversation_history[0] == {"role": "callee", "text": "yes"}

    @pytest.mark.asyncio
    async def test_terminal_turn_flagged(self, sop_engine, session):
        session.current_step = "confirm_resolution"

        async def fake_reason(_request):
            return AgentResponse(
                next_step="close_resolved",
                utterance="Perfect, thank you.",
                reasoning="resolved",
                outcome="resolved",
            )

        convo = StreamingConversation(sop_engine, session, reason_fn=fake_reason)
        turn = await convo.respond_to("yes")
        assert turn.is_terminal is True
