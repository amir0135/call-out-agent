"""Tests for the SOP Execution Engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Adjust import path for test context
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.models import (
    AgentResponse,
    AlarmContext,
    CallIntent,
    CallOutcome,
    CallSession,
)
from orchestrator.sop_engine import SOPEngine


@pytest.fixture
def sop_engine():
    engine = SOPEngine()
    engine.load()
    return engine


@pytest.fixture
def alarm_context():
    return AlarmContext(
        alarm_id="ALM-001",
        alarm_type="high_temperature",
        severity="high",
        store_name="Test Store",
        store_id="ST-001",
        equipment_name="Cooler Unit A",
        current_temp=12.5,
        threshold_temp=8.0,
        alarm_time="2026-04-16T10:30:00Z",
        customer_id="CUST-001",
    )


@pytest.fixture
def call_session(alarm_context):
    intent = CallIntent(
        intent_id="INT-001",
        alarm=alarm_context,
        phone_number="+1234567890",
    )
    return CallSession(call_id="CALL-001", intent=intent)


class TestSOPLoading:
    def test_load_sop(self, sop_engine):
        assert sop_engine.definition.sop_id == "SOP-HTA-001"
        assert sop_engine.definition.alarm_type == "high_temperature"

    def test_sop_has_steps(self, sop_engine):
        assert len(sop_engine.definition.steps) > 0

    def test_sop_has_terminal_outcomes(self, sop_engine):
        assert "resolved" in sop_engine.definition.terminal_outcomes
        assert "retry_later" in sop_engine.definition.terminal_outcomes
        assert "escalate_human" in sop_engine.definition.terminal_outcomes


class TestStepAccess:
    def test_get_greeting_step(self, sop_engine, call_session):
        step = sop_engine.get_current_step(call_session)
        assert step.id == "greeting"
        assert step.type.value == "speak"

    def test_get_step_by_id(self, sop_engine):
        step = sop_engine.get_step("ask_awareness")
        assert step.id == "ask_awareness"
        assert step.type.value == "ask"
        assert len(step.branches) == 3

    def test_invalid_step_raises(self, sop_engine):
        with pytest.raises(ValueError, match="Unknown SOP step"):
            sop_engine.get_step("nonexistent_step")


class TestTextRendering:
    def test_render_greeting(self, sop_engine, call_session):
        step = sop_engine.get_current_step(call_session)
        text = sop_engine.render_text(step, call_session)
        assert "Test Store" in text
        assert "temperature alarm" in text

    def test_render_alarm_details(self, sop_engine, call_session):
        # inform_details carries the temperature variables; verify they render.
        step = sop_engine.get_step("inform_details")
        text = sop_engine.render_text(step, call_session)
        assert "Cooler Unit A" in text
        assert "12.5" in text
        assert "8.0" in text


class TestTransitions:
    def test_speak_step_advances(self, sop_engine, call_session):
        """Greeting (speak) should advance to ask_awareness."""
        response = AgentResponse(
            next_step="ask_awareness",
            utterance="test",
            reasoning="greeting done",
        )
        next_step_id, outcome = sop_engine.evaluate_transition(call_session, response)
        assert next_step_id == "ask_awareness"
        assert outcome is None

    def test_ask_step_branch_yes(self, sop_engine, call_session):
        """ask_awareness → yes → ask_action_taken."""
        call_session.current_step = "ask_awareness"
        response = AgentResponse(
            next_step="ask_action_taken",
            utterance="test",
            reasoning="callee aware",
        )
        next_step_id, outcome = sop_engine.evaluate_transition(call_session, response)
        assert next_step_id == "ask_action_taken"

    def test_ask_step_branch_no(self, sop_engine, call_session):
        """ask_awareness → no → inform_details."""
        call_session.current_step = "ask_awareness"
        response = AgentResponse(
            next_step="inform_details",
            utterance="test",
            reasoning="callee not aware",
        )
        next_step_id, outcome = sop_engine.evaluate_transition(call_session, response)
        assert next_step_id == "inform_details"

    def test_invalid_branch_target_escalates(self, sop_engine, call_session):
        """Agent proposes a step not in the branch list → escalate."""
        call_session.current_step = "ask_awareness"
        response = AgentResponse(
            next_step="close_resolved",
            utterance="test",
            reasoning="invalid jump",
        )
        next_step_id, outcome = sop_engine.evaluate_transition(call_session, response)
        assert next_step_id == "escalate_unclear"
        assert outcome == "escalate_human"

    def test_terminal_step_resolved(self, sop_engine, call_session):
        """close_resolved is a terminal step with outcome=resolved."""
        call_session.current_step = "confirm_resolution"
        response = AgentResponse(
            next_step="close_resolved",
            utterance="test",
            reasoning="resolved",
        )
        next_step_id, outcome = sop_engine.evaluate_transition(call_session, response)
        assert next_step_id == "close_resolved"
        assert outcome == "resolved"

    def test_agent_escalation_overrides(self, sop_engine, call_session):
        """Agent requesting escalation overrides normal flow."""
        call_session.current_step = "ask_awareness"
        response = AgentResponse(
            next_step="ask_action_taken",
            utterance="test",
            reasoning="uncertain",
            should_escalate=True,
        )
        next_step_id, outcome = sop_engine.evaluate_transition(call_session, response)
        assert next_step_id == "escalate_unclear"
        assert outcome == "escalate_human"


class TestSessionAdvancement:
    def test_advance_through_greeting(self, sop_engine, call_session):
        response = AgentResponse(
            next_step="ask_awareness",
            utterance="test",
            reasoning="greeting done",
        )
        next_step, outcome = sop_engine.advance_session(call_session, response)
        assert next_step is not None
        assert next_step.id == "ask_awareness"
        assert call_session.current_step == "ask_awareness"
        assert outcome is None

    def test_advance_to_terminal(self, sop_engine, call_session):
        call_session.current_step = "confirm_resolution"
        response = AgentResponse(
            next_step="close_resolved",
            utterance="Thank you.",
            reasoning="resolved",
            outcome="resolved",
        )
        next_step, outcome = sop_engine.advance_session(call_session, response)
        assert outcome == "resolved"
        assert call_session.outcome == CallOutcome.RESOLVED


class TestUnclearTracking:
    def test_unclear_threshold(self, sop_engine, call_session):
        assert not sop_engine.should_escalate_unclear(call_session)
        sop_engine.increment_unclear(call_session)
        assert not sop_engine.should_escalate_unclear(call_session)
        sop_engine.increment_unclear(call_session)
        assert sop_engine.should_escalate_unclear(call_session)

    def test_terminal_detection(self, sop_engine):
        assert sop_engine.is_terminal("close_resolved")
        assert sop_engine.is_terminal("escalate_human")
        assert not sop_engine.is_terminal("ask_awareness")
        assert sop_engine.is_terminal(None)
