"""Domain models for the Call-Out Agent orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CallOutcome(str, Enum):
    """Terminal outcomes for a call-out session."""

    RESOLVED = "resolved"
    RETRY_LATER = "retry_later"
    ESCALATE_HUMAN = "escalate_human"
    FAILED = "failed"
    NO_ANSWER = "no_answer"


class SOPStepType(str, Enum):
    """Type of SOP step."""

    SPEAK = "speak"
    ASK = "ask"


class SOPBranch(BaseModel):
    """A branch transition in an SOP ask step."""

    match: str
    keywords: list[str] = Field(default_factory=list)
    next: str


class SOPStep(BaseModel):
    """A single step in the SOP definition."""

    id: str
    type: SOPStepType
    text: str
    next: str | None = None
    branches: list[SOPBranch] | None = None
    outcome: str | None = None
    # Hint for the recognizer: "yes_no" / "numeric" steps use a tighter
    # end_silence_timeout (snappier turn-taking) while "open" / None
    # uses the conversational default. Set per step in the SOP JSON.
    expected_response: str | None = None


class SOPDefinition(BaseModel):
    """Full SOP definition loaded from JSON."""

    sop_id: str
    name: str
    version: str
    alarm_type: str
    description: str
    constraints: dict[str, Any]
    steps: list[SOPStep]
    terminal_outcomes: list[str]

    def get_step(self, step_id: str) -> SOPStep | None:
        """Get a step by its ID."""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None


class AlarmContext(BaseModel):
    """Alarm information that triggers a call-out."""

    alarm_id: str
    alarm_type: str
    severity: str = "high"
    store_name: str
    store_id: str
    equipment_name: str
    current_temp: float
    threshold_temp: float
    alarm_time: str
    customer_id: str


class CallIntent(BaseModel):
    """A request to place an outbound call."""

    intent_id: str = Field(default_factory=lambda: "")
    alarm: AlarmContext
    phone_number: str | None = None
    sop_id: str = "SOP-HTA-001"
    customer_id: str | None = None  # convenience override; falls back to alarm.customer_id
    priority: int = 1
    # Optional per-alarm result webhook. Overrides OUTCOME_WEBHOOK_URL so
    # different upstream systems can route their own call outcomes.
    callback_url: str | None = None

    @property
    def resolved_customer_id(self) -> str:
        return self.customer_id or self.alarm.customer_id


class AgentRequest(BaseModel):
    """Request sent to the Foundry hosted agent for SOP reasoning."""

    alarm_context: dict[str, Any]
    sop_definition: dict[str, Any]
    conversation_history: list[dict[str, str]]
    current_step: str
    customer_id: str | None = None
    unclear_count: int = 0


class AgentResponse(BaseModel):
    """Response from the Foundry hosted agent."""

    next_step: str
    utterance: str
    reasoning: str
    should_escalate: bool = False
    outcome: str | None = None
    knowledge_used: list[str] | None = None


class AuditEventType(str, Enum):
    """Types of audit events."""

    CALL_INITIATED = "call_initiated"
    CALL_CONNECTED = "call_connected"
    CALL_DISCONNECTED = "call_disconnected"
    SOP_STEP_EXECUTED = "sop_step_executed"
    SPEECH_RECOGNIZED = "speech_recognized"
    AGENT_RESPONSE = "agent_response"
    ESCALATION_TRIGGERED = "escalation_triggered"
    CALL_COMPLETED = "call_completed"
    CALL_FAILED = "call_failed"
    RETRY_SCHEDULED = "retry_scheduled"
    RECORDING_AVAILABLE = "recording_available"


class AuditEvent(BaseModel):
    """An auditable event in the call-out lifecycle."""

    id: str = Field(default_factory=lambda: "")
    call_id: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event_type: AuditEventType
    sop_step: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class CallSession(BaseModel):
    """Tracks the state of an active call-out session."""

    call_id: str
    intent: CallIntent
    current_step: str = "greeting"
    conversation_history: list[dict[str, str]] = Field(default_factory=list)
    unclear_count: int = 0
    attempt_number: int = 1
    outcome: CallOutcome | None = None
    # Free-form annotations (e.g. the s2s engines' model-reported
    # outcome_summary). Persisted with the session.
    metadata: dict[str, str] = Field(default_factory=dict)
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ended_at: str | None = None
    completion_logged: bool = False
    recording_id: str | None = None
    recording_url: str | None = None
    server_call_id: str | None = None
    # Region (from RegionRouter) this call is placed in — selects the ACS
    # resource, caller-ID number, and Speech region nearest the callee.
    # Persisted so any replica can operate the call's connection correctly.
    region_id: str | None = None
    # ACS webhooks are at-least-once; keep a bounded set of processed
    # event IDs so duplicate deliveries do not re-run call transitions.
    processed_event_ids: list[str] = Field(default_factory=list)
