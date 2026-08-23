"""Audit logger — writes call lifecycle events to Cosmos DB or stdout (local mode)."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from .models import AuditEvent, AuditEventType, CallSession

logger = logging.getLogger(__name__)

LOCAL_MODE = os.environ.get("LOCAL_MODE", "false").lower() in ("true", "1", "yes")


class AuditLogger:
    """Writes structured audit events to Cosmos DB (or stdout in local mode).

    Every call lifecycle event — initiation, SOP step execution, speech
    recognition results, agent responses, outcomes — is persisted for
    compliance and operational review.
    """

    def __init__(
        self,
        cosmos_endpoint: str = "",
        database_name: str = "callout",
        container_name: str = "audit_events",
    ):
        self._local_mode = LOCAL_MODE or not cosmos_endpoint

        if not self._local_mode:
            from azure.cosmos import CosmosClient
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
            self._client = CosmosClient(url=cosmos_endpoint, credential=credential)
            self._database = self._client.get_database_client(database_name)
            self._container = self._database.get_container_client(container_name)

    def log_event(self, event: AuditEvent) -> None:
        """Write a single audit event."""
        if not event.id:
            event.id = str(uuid.uuid4())
        if not event.timestamp:
            event.timestamp = datetime.now(timezone.utc).isoformat()

        if self._local_mode:
            logger.info(
                "AUDIT | %s | call=%s step=%s | %s",
                event.event_type.value,
                event.call_id,
                event.sop_step or "-",
                json.dumps(event.data, default=str)[:200],
            )
            return

        item = event.model_dump()
        item["id"] = event.id
        item["callId"] = event.call_id

        try:
            self._container.create_item(body=item)
            logger.debug(
                "Audit event %s logged for call %s",
                event.event_type.value,
                event.call_id,
            )
        except CosmosResourceExistsError:
            logger.warning("Duplicate audit event %s", event.id)
        except Exception:
            logger.exception("Failed to write audit event %s", event.id)

    # ------------------------------------------------------------------
    # Convenience methods for common events
    # ------------------------------------------------------------------

    def log_call_initiated(self, session: CallSession) -> None:
        self.log_event(
            AuditEvent(
                call_id=session.call_id,
                event_type=AuditEventType.CALL_INITIATED,
                data={
                    "alarm_id": session.intent.alarm.alarm_id,
                    "phone_number": session.intent.phone_number,
                    "store_name": session.intent.alarm.store_name,
                    "attempt": session.attempt_number,
                },
            )
        )

    def log_call_connected(self, session: CallSession) -> None:
        self.log_event(
            AuditEvent(
                call_id=session.call_id,
                event_type=AuditEventType.CALL_CONNECTED,
                sop_step=session.current_step,
                data={"server_call_id": session.server_call_id},
            )
        )

    def log_sop_step(self, session: CallSession, utterance: str) -> None:
        self.log_event(
            AuditEvent(
                call_id=session.call_id,
                event_type=AuditEventType.SOP_STEP_EXECUTED,
                sop_step=session.current_step,
                data={"utterance": utterance},
            )
        )

    def log_speech_recognized(self, session: CallSession, transcript: str) -> None:
        self.log_event(
            AuditEvent(
                call_id=session.call_id,
                event_type=AuditEventType.SPEECH_RECOGNIZED,
                sop_step=session.current_step,
                data={"transcript": transcript},
            )
        )

    def log_agent_response(
        self,
        session: CallSession,
        next_step: str,
        reasoning: str,
    ) -> None:
        self.log_event(
            AuditEvent(
                call_id=session.call_id,
                event_type=AuditEventType.AGENT_RESPONSE,
                sop_step=session.current_step,
                data={"next_step": next_step, "reasoning": reasoning},
            )
        )

    def log_escalation(self, session: CallSession, reason: str) -> None:
        self.log_event(
            AuditEvent(
                call_id=session.call_id,
                event_type=AuditEventType.ESCALATION_TRIGGERED,
                sop_step=session.current_step,
                data={"reason": reason},
            )
        )

    def log_call_completed(self, session: CallSession) -> None:
        self.log_event(
            AuditEvent(
                call_id=session.call_id,
                event_type=AuditEventType.CALL_COMPLETED,
                sop_step=session.current_step,
                data={
                    "outcome": session.outcome.value if session.outcome else "unknown",
                    "recording_id": session.recording_id,
                    "recording_url": session.recording_url,
                    "duration_steps": len(session.conversation_history),
                },
            )
        )

    def log_recording(self, session: CallSession, recording_url: str) -> None:
        self.log_event(
            AuditEvent(
                call_id=session.call_id,
                event_type=AuditEventType.RECORDING_AVAILABLE,
                data={"recording_url": recording_url},
            )
        )
