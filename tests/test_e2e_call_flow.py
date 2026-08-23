"""Integration test — simulated end-to-end call flow."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.models import (
    AgentResponse,
    AlarmContext,
    CallIntent,
    CallOutcome,
    CallSession,
)
from orchestrator.retry_manager import RetryManager
from orchestrator.sop_engine import SOPEngine


@pytest.fixture
def sop_engine():
    engine = SOPEngine()
    engine.load()
    return engine


@pytest.fixture
def call_session():
    alarm = AlarmContext(
        alarm_id="ALM-E2E-001",
        alarm_type="high_temperature",
        severity="high",
        store_name="E2E Test Store",
        store_id="ST-E2E",
        equipment_name="Walk-in Cooler B",
        current_temp=15.0,
        threshold_temp=8.0,
        alarm_time="2026-04-16T11:00:00Z",
        customer_id="CUST-E2E",
    )
    intent = CallIntent(
        intent_id="INT-E2E",
        alarm=alarm,
        phone_number="+1555000000",
    )
    return CallSession(call_id="CALL-E2E-001", intent=intent)


class TestEndToEndCallFlow:
    """Simulate a complete call flow through the SOP engine."""

    def test_happy_path_resolved(self, sop_engine, call_session):
        """Full flow: greeting → aware → action taken → resolved."""

        # Step 1: Greeting (speak → auto-advance)
        step = sop_engine.get_current_step(call_session)
        assert step.id == "greeting"
        text = sop_engine.render_text(step, call_session)
        assert "E2E Test Store" in text
        call_session.conversation_history.append({"role": "agent", "text": text})

        # Advance past greeting
        call_session.current_step = "ask_awareness"

        # Step 2: Ask awareness → callee says yes
        step = sop_engine.get_current_step(call_session)
        assert step.id == "ask_awareness"
        call_session.conversation_history.append(
            {"role": "callee", "text": "Yes, we noticed the alarm this morning."}
        )

        response = AgentResponse(
            next_step="ask_action_taken",
            utterance="Has any action been taken?",
            reasoning="Callee is aware of the alarm",
        )
        next_step, outcome = sop_engine.advance_session(call_session, response)
        assert next_step.id == "ask_action_taken"

        # Step 3: Ask action → callee says yes
        call_session.conversation_history.append(
            {"role": "callee", "text": "Yes, our technician checked it and reset the unit."}
        )

        response = AgentResponse(
            next_step="confirm_resolution",
            utterance="Can you confirm this alarm will be monitored?",
            reasoning="Action was taken, confirming resolution",
        )
        next_step, outcome = sop_engine.advance_session(call_session, response)
        assert next_step.id == "confirm_resolution"

        # Step 4: Confirm resolution → resolved
        call_session.conversation_history.append(
            {"role": "callee", "text": "Yes, we'll keep an eye on it. It's handled."}
        )

        response = AgentResponse(
            next_step="close_resolved",
            utterance="Thank you. We will continue monitoring.",
            reasoning="Callee confirmed alarm is handled",
            outcome="resolved",
        )
        next_step, outcome = sop_engine.advance_session(call_session, response)
        assert outcome == "resolved"
        assert call_session.outcome == CallOutcome.RESOLVED

    def test_unaware_path_with_follow_up(self, sop_engine, call_session):
        """Flow: greeting → not aware → inform → no action → recommend → follow up."""

        call_session.current_step = "ask_awareness"

        # Not aware → goes to inform_details
        response = AgentResponse(
            next_step="inform_details",
            utterance="The alarm was triggered...",
            reasoning="Callee not aware",
        )
        next_step, outcome = sop_engine.advance_session(call_session, response)
        assert next_step.id == "inform_details"

        # inform_details is speak → should auto-advance to ask_action_taken
        assert next_step.next == "ask_action_taken"

        call_session.current_step = "ask_action_taken"

        # No action taken → recommend
        response = AgentResponse(
            next_step="recommend_action",
            utterance="I recommend inspection.",
            reasoning="No action taken yet",
        )
        next_step, outcome = sop_engine.advance_session(call_session, response)
        assert next_step.id == "recommend_action"

        # recommend is speak → advances to confirm_resolution
        call_session.current_step = "confirm_resolution"

        # Callee wants follow up
        response = AgentResponse(
            next_step="close_retry",
            utterance="We will follow up later.",
            reasoning="Callee needs time",
            outcome="retry_later",
        )
        next_step, outcome = sop_engine.advance_session(call_session, response)
        assert outcome == "retry_later"
        assert call_session.outcome == CallOutcome.RETRY_LATER

    def test_escalation_path(self, sop_engine, call_session):
        """Flow: unclear responses → escalation."""

        call_session.current_step = "ask_awareness"

        # First unclear
        sop_engine.increment_unclear(call_session)
        assert not sop_engine.should_escalate_unclear(call_session)

        # Second unclear → threshold reached
        sop_engine.increment_unclear(call_session)
        assert sop_engine.should_escalate_unclear(call_session)

        # Agent should escalate
        response = AgentResponse(
            next_step="escalate_unclear",
            utterance="Connecting to human operator.",
            reasoning="Max unclear responses exceeded",
            should_escalate=True,
            outcome="escalate_human",
        )
        next_step, outcome = sop_engine.advance_session(call_session, response)
        assert outcome == "escalate_human"
        assert call_session.outcome == CallOutcome.ESCALATE_HUMAN

    def test_retry_manager_integration(self, sop_engine, call_session):
        """Test retry manager with SOP outcomes."""
        manager = RetryManager(max_retries=3)

        # First attempt fails
        manager.record_outcome("ALM-E2E-001", CallOutcome.NO_ANSWER)
        should_call, reason = manager.should_attempt_call(call_session.intent)
        # May depend on time window
        if should_call:
            assert "2/3" in reason

        # Second attempt fails
        manager.record_outcome("ALM-E2E-001", CallOutcome.FAILED)

        # Third attempt — resolved
        manager.record_outcome("ALM-E2E-001", CallOutcome.RESOLVED)
        should_call, _ = manager.should_attempt_call(call_session.intent)
        assert not should_call
