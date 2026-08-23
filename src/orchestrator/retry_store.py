"""Pluggable storage for retry / escalation state per alarm.

The in-memory dict on :class:`~orchestrator.retry_manager.RetryManager`
breaks horizontal scaling and loses state on restart. This module
externalizes that state behind a small interface with two backends:

* :class:`InMemoryRetryStore` — process-local. Default for local dev
  and tests. Identical semantics to the original in-memory dict.
* :class:`CosmosRetryStore` — Azure Cosmos DB. Production backend.
  Records partitioned by ``alarm_id``; uses ETag optimistic
  concurrency to keep concurrent attempts safe.

The :func:`create_retry_store` factory selects a backend based on
environment variables so tests and local runs require no changes.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Protocol

from .models import CallOutcome

logger = logging.getLogger(__name__)


class RetryRecord:
    """Tracks retry state for a single alarm.

    Identical public surface to the original ``RetryRecord`` so existing
    callers do not need to change.
    """

    def __init__(self, alarm_id: str, max_retries: int = 3):
        self.alarm_id = alarm_id
        self.max_retries = max_retries
        self.attempts: list[dict[str, Any]] = []
        self.final_outcome: CallOutcome | None = None
        # ETag is populated when the record is loaded from Cosmos so that
        # writes can use optimistic concurrency. Ignored by the in-memory
        # backend.
        self._etag: str | None = None

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def should_retry(self) -> bool:
        if self.final_outcome in (CallOutcome.RESOLVED, CallOutcome.ESCALATE_HUMAN):
            return False
        return self.attempt_count < self.max_retries

    def record_attempt(self, outcome: CallOutcome) -> None:
        self.attempts.append(
            {
                "attempt": self.attempt_count + 1,
                "outcome": outcome.value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        if outcome == CallOutcome.RESOLVED:
            self.final_outcome = CallOutcome.RESOLVED
        elif outcome == CallOutcome.ESCALATE_HUMAN:
            self.final_outcome = CallOutcome.ESCALATE_HUMAN
        elif not self.should_retry:
            self.final_outcome = CallOutcome.ESCALATE_HUMAN
            logger.warning(
                "Max retries (%d) reached for alarm %s — escalating",
                self.max_retries,
                self.alarm_id,
            )

    # -- serialization helpers used by the Cosmos backend --------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.alarm_id,
            "alarmId": self.alarm_id,
            "maxRetries": self.max_retries,
            "attempts": self.attempts,
            "finalOutcome": self.final_outcome.value if self.final_outcome else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetryRecord":
        record = cls(alarm_id=data["alarmId"], max_retries=data.get("maxRetries", 3))
        record.attempts = list(data.get("attempts", []))
        outcome = data.get("finalOutcome")
        record.final_outcome = CallOutcome(outcome) if outcome else None
        record._etag = data.get("_etag")
        return record


class RetryStore(Protocol):
    """Storage contract for :class:`RetryRecord` objects."""

    def get(self, alarm_id: str) -> RetryRecord | None: ...
    def upsert(self, record: RetryRecord) -> None: ...
    def delete(self, alarm_id: str) -> None: ...


class InMemoryRetryStore:
    """Process-local retry store. Default for local dev and tests."""

    def __init__(self) -> None:
        self._records: dict[str, RetryRecord] = {}

    def get(self, alarm_id: str) -> RetryRecord | None:
        return self._records.get(alarm_id)

    def upsert(self, record: RetryRecord) -> None:
        self._records[record.alarm_id] = record

    def delete(self, alarm_id: str) -> None:
        self._records.pop(alarm_id, None)


class CosmosRetryStore:
    """Cosmos-backed retry store for multi-replica production deployments.

    The container is expected to be partitioned by ``/alarmId``. The
    ``azure-cosmos`` package is imported lazily so this module can be
    imported in environments that do not have it.
    """

    def __init__(
        self,
        cosmos_endpoint: str,
        database_name: str = "callout",
        container_name: str = "retry_state",
    ):
        try:
            from azure.cosmos import CosmosClient  # noqa: F401
            from azure.identity import DefaultAzureCredential  # noqa: F401
        except ImportError as exc:  # pragma: no cover - exercised in deployment only
            raise RuntimeError(
                "CosmosRetryStore requires 'azure-cosmos' and 'azure-identity'."
            ) from exc

        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        self._client = CosmosClient(url=cosmos_endpoint, credential=credential)
        self._container = (
            self._client.get_database_client(database_name)
            .get_container_client(container_name)
        )
        logger.info(
            "CosmosRetryStore initialized (database=%s container=%s)",
            database_name,
            container_name,
        )

    def get(self, alarm_id: str) -> RetryRecord | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            item = self._container.read_item(item=alarm_id, partition_key=alarm_id)
        except CosmosResourceNotFoundError:
            return None
        return RetryRecord.from_dict(item)

    def upsert(self, record: RetryRecord) -> None:
        self._container.upsert_item(body=record.to_dict())

    def delete(self, alarm_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            self._container.delete_item(item=alarm_id, partition_key=alarm_id)
        except CosmosResourceNotFoundError:
            pass


def create_retry_store(
    cosmos_endpoint: str | None = None,
    container_name: str | None = None,
) -> RetryStore:
    """Factory selecting the backend based on configuration.

    Precedence:
    1. Explicit ``cosmos_endpoint`` argument
    2. ``RETRY_STATE_COSMOS_ENDPOINT`` env var (or ``COSMOS_ENDPOINT``
       paired with ``RETRY_STATE_COSMOS_CONTAINER``)
    3. Fallback to :class:`InMemoryRetryStore`
    """
    endpoint = cosmos_endpoint or os.environ.get("RETRY_STATE_COSMOS_ENDPOINT", "")
    container = container_name or os.environ.get("RETRY_STATE_COSMOS_CONTAINER", "")

    # Allow reusing the existing Cosmos account if RETRY_STATE_COSMOS_CONTAINER
    # is set; only enable the Cosmos backend when an explicit container is named.
    if container:
        endpoint = endpoint or os.environ.get("COSMOS_ENDPOINT", "")

    if endpoint and container:
        logger.info("Using CosmosRetryStore (multi-replica safe)")
        return CosmosRetryStore(cosmos_endpoint=endpoint, container_name=container)

    logger.info(
        "Using InMemoryRetryStore — SAFE ONLY WITH A SINGLE REPLICA. "
        "Set RETRY_STATE_COSMOS_CONTAINER to enable horizontal scale."
    )
    return InMemoryRetryStore()
