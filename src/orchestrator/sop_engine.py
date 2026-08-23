"""SOP Execution Engine — constrains the AI to follow SOPs exactly."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .models import AgentResponse, CallSession, SOPDefinition, SOPStep, SOPStepType

logger = logging.getLogger(__name__)

_SOP_DIR = Path(__file__).resolve().parent.parent.parent / "sops"


class SOPEngine:
    """Loads SOP definitions and tracks execution state per call session.

    The engine is the safety boundary — it decides which step is current,
    evaluates branch transitions, and detects terminal states.
    The LLM never chooses a step outside what the engine allows.

    A SOPCatalog can be injected to enable per-customer SOP resolution
    (base + JSON-Patch overlays). Falls back to single-file load when not.
    """

    def __init__(
        self,
        sop_path: str | Path | None = None,
        catalog: Any | None = None,
    ):
        if sop_path is None:
            sop_path = _SOP_DIR / "high_temp_alarm.json"
        self._sop_path = Path(sop_path)
        self._definition: SOPDefinition | None = None
        self._catalog = catalog

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> SOPDefinition:
        """Load the default SOP definition from disk (legacy path)."""
        with open(self._sop_path, encoding="utf-8") as f:
            data = json.load(f)
        self._definition = SOPDefinition(**data)
        logger.info("Loaded SOP %s v%s", self._definition.sop_id, self._definition.version)
        return self._definition

    @property
    def definition(self) -> SOPDefinition:
        if self._definition is None:
            self.load()
        assert self._definition is not None
        return self._definition

    def set_catalog(self, catalog: Any) -> None:
        """Inject (or replace) the SOPCatalog used for per-customer resolution."""
        self._catalog = catalog

    def definition_for(self, session: CallSession) -> SOPDefinition:
        """Return the SOP definition that applies to this session.

        Uses the catalog (with customer overlays) when available; otherwise
        falls back to the single loaded definition.
        """
        if self._catalog is not None:
            sop_id = session.intent.sop_id
            customer_id = session.intent.resolved_customer_id
            return self._catalog.resolve(sop_id, customer_id)
        return self.definition

    # ------------------------------------------------------------------
    # Step accessors
    # ------------------------------------------------------------------

    def get_current_step(self, session: CallSession) -> SOPStep:
        """Return the current SOP step for a call session."""
        step = self.definition_for(session).get_step(session.current_step)
        if step is None:
            raise ValueError(f"Unknown SOP step: {session.current_step}")
        return step

    def get_step(self, step_id: str, session: CallSession | None = None) -> SOPStep:
        defn = self.definition_for(session) if session is not None else self.definition
        step = defn.get_step(step_id)
        if step is None:
            raise ValueError(f"Unknown SOP step: {step_id}")
        return step

    # ------------------------------------------------------------------
    # Template rendering
    # ------------------------------------------------------------------

    def render_text(self, step: SOPStep, session: CallSession) -> str:
        """Render step text with alarm context variables."""
        alarm = session.intent.alarm
        return step.text.format(
            store_name=alarm.store_name,
            equipment_name=alarm.equipment_name,
            current_temp=alarm.current_temp,
            threshold_temp=alarm.threshold_temp,
            alarm_time=alarm.alarm_time,
        )

    # ------------------------------------------------------------------
    # Transition logic
    # ------------------------------------------------------------------

    def evaluate_transition(
        self,
        session: CallSession,
        agent_response: AgentResponse,
    ) -> tuple[str | None, str | None]:
        """Evaluate the agent's response and determine the next step.

        Returns (next_step_id, outcome).  If outcome is set, the call is terminal.
        """
        current = self.get_current_step(session)

        # Agent requested escalation — override the normal flow
        if agent_response.should_escalate:
            logger.warning("Agent requested escalation at step %s", current.id)
            return "escalate_unclear", "escalate_human"

        # Validate that the agent's proposed next step actually exists
        proposed = agent_response.next_step
        proposed_step = self.definition_for(session).get_step(proposed)

        if proposed_step is None:
            logger.error(
                "Agent proposed invalid step '%s' — escalating", proposed
            )
            return "escalate_unclear", "escalate_human"

        # For speak steps, next is deterministic — use the SOP definition
        if current.type == SOPStepType.SPEAK:
            if current.outcome:
                return None, current.outcome
            if current.next:
                return current.next, None
            # Fallback: trust agent if SOP has no explicit next
            return proposed, proposed_step.outcome

        # For ask steps, validate the proposed step is a legal branch target
        if current.branches:
            valid_targets = {b.next for b in current.branches}
            if proposed in valid_targets:
                target_step = self.get_step(proposed)
                return proposed, target_step.outcome
            # Agent proposed something outside allowed branches
            logger.warning(
                "Agent proposed '%s' which is not a valid branch from '%s' — valid: %s",
                proposed,
                current.id,
                valid_targets,
            )
            return "escalate_unclear", "escalate_human"

        # No branches defined — follow agent
        return proposed, proposed_step.outcome

    # ------------------------------------------------------------------
    # Recognition tuning per step
    # ------------------------------------------------------------------

    @staticmethod
    def recognize_timeout_for(
        step: SOPStep,
        fast_seconds: float,
        slow_seconds: float,
    ) -> float:
        """Pick an end_silence_timeout based on the SOP step's hint.

        Short-answer prompts ("yes_no", "numeric") finalize STT faster
        so the conversation feels snappier; open-ended prompts (the
        default) keep the longer, more forgiving timeout so they don't
        truncate real answers.
        """
        hint = (step.expected_response or "").lower()
        if hint in {"yes_no", "numeric", "digit", "short"}:
            return fast_seconds
        return slow_seconds

    def is_terminal(self, step_id: str | None, session: CallSession | None = None) -> bool:
        """Check if a step is a terminal state (no next step, has outcome)."""
        if step_id is None:
            return True
        defn = self.definition_for(session) if session is not None else self.definition
        step = defn.get_step(step_id)
        if step is None:
            return True
        return step.outcome is not None and step.next is None

    def advance_session(
        self,
        session: CallSession,
        agent_response: AgentResponse,
    ) -> tuple[SOPStep | None, str | None]:
        """Advance the session to the next step.

        Returns (next_step, outcome).  If next_step is None, the call is complete.
        """
        next_step_id, outcome = self.evaluate_transition(session, agent_response)

        if next_step_id is not None:
            session.current_step = next_step_id

        if outcome is not None:
            from .models import CallOutcome

            try:
                session.outcome = CallOutcome(outcome)
            except ValueError:
                session.outcome = CallOutcome.FAILED

        if next_step_id is None:
            return None, outcome

        next_step = self.get_step(next_step_id)
        return next_step, outcome

    # ------------------------------------------------------------------
    # Unclear response tracking
    # ------------------------------------------------------------------

    def should_escalate_unclear(self, session: CallSession) -> bool:
        """Check if unclear response threshold is exceeded."""
        max_unclear = self.definition_for(session).constraints.get("max_unclear_responses", 2)
        return session.unclear_count >= max_unclear

    def increment_unclear(self, session: CallSession) -> None:
        """Increment the unclear response counter."""
        session.unclear_count += 1
        logger.info(
            "Unclear response count for call %s: %d",
            session.call_id,
            session.unclear_count,
        )
