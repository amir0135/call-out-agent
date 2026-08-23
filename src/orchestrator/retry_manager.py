"""Retry manager — tracks call attempts, enforces time windows, triggers escalation.

State is delegated to a :class:`~orchestrator.retry_store.RetryStore` so
the manager is safe to run across multiple orchestrator replicas. The
default (in-memory) backend preserves the original single-process
behaviour for local development and unit tests.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone

from .models import CallIntent, CallOutcome
from .retry_store import InMemoryRetryStore, RetryRecord, RetryStore

logger = logging.getLogger(__name__)

# Re-export ``RetryRecord`` so callers that imported it from this module
# (its previous home) continue to work.
__all__ = ["RetryManager", "RetryRecord"]


class RetryManager:
    """Manages retry logic across all active alarms.

    Enforces:
    - Maximum retry attempts per alarm
    - Call time windows (e.g. 06:00–22:00)
    - Escalation after exhausted retries
    """

    def __init__(
        self,
        max_retries: int = 3,
        call_window_start: str = "06:00",
        call_window_end: str = "22:00",
        store: RetryStore | None = None,
    ):
        self._max_retries = max_retries
        self._window_start = self._parse_time(call_window_start)
        self._window_end = self._parse_time(call_window_end)
        # Pluggable retry store — in-memory by default for backward
        # compatibility. Inject a CosmosRetryStore in production.
        self._store: RetryStore = store or InMemoryRetryStore()

    @staticmethod
    def _parse_time(t: str) -> time:
        parts = t.split(":")
        return time(int(parts[0]), int(parts[1]))

    def is_within_call_window(self) -> bool:
        """Check if current time is within the allowed call window."""
        now = datetime.now(timezone.utc).time()
        return self._window_start <= now <= self._window_end

    def get_or_create_record(self, alarm_id: str) -> RetryRecord:
        """Get existing retry record or create a new one."""
        record = self._store.get(alarm_id)
        if record is None:
            record = RetryRecord(
                alarm_id=alarm_id,
                max_retries=self._max_retries,
            )
            self._store.upsert(record)
        return record

    def get_record(self, alarm_id: str) -> RetryRecord | None:
        """Read-only lookup (no create) — used by the status API."""
        return self._store.get(alarm_id)

    def should_attempt_call(self, intent: CallIntent) -> tuple[bool, str]:
        """Decide whether to attempt a call for this alarm.

        Returns (should_call, reason).
        """
        record = self.get_or_create_record(intent.alarm.alarm_id)

        if record.final_outcome == CallOutcome.RESOLVED:
            return False, "Alarm already resolved"

        if record.final_outcome == CallOutcome.ESCALATE_HUMAN:
            return False, "Already escalated to human"

        if not record.should_retry:
            return False, f"Max retries ({self._max_retries}) exhausted"

        if not self.is_within_call_window():
            return False, (
                f"Outside call window ({self._window_start}–{self._window_end})"
            )

        return True, f"Attempt {record.attempt_count + 1}/{self._max_retries}"

    def record_outcome(self, alarm_id: str, outcome: CallOutcome) -> RetryRecord:
        """Record the outcome of a call attempt."""
        record = self.get_or_create_record(alarm_id)
        record.record_attempt(outcome)
        # Persist the mutation. In-memory store is a no-op upsert; Cosmos
        # writes the updated document.
        self._store.upsert(record)
        logger.info(
            "Call outcome for alarm %s: %s (attempt %d/%d)",
            alarm_id,
            outcome.value,
            record.attempt_count,
            self._max_retries,
        )
        return record

    def get_next_attempt_number(self, alarm_id: str) -> int:
        """Get the next attempt number for an alarm."""
        record = self.get_or_create_record(alarm_id)
        return record.attempt_count + 1

    def clear_record(self, alarm_id: str) -> None:
        """Remove a retry record (alarm resolved or cleaned up)."""
        self._store.delete(alarm_id)

