"""Pluggable storage for active call sessions.

The orchestrator runs as multiple replicas behind a load balancer; ACS
webhook callbacks for a given call can land on any replica. We therefore
cannot keep `CallSession` objects in process memory — they must live in
a shared store accessible by every replica.

Two backends are provided:

* :class:`InMemorySessionStore` — process-local dict. Used for local
  development and unit tests. Safe only when running with a single
  replica.
* :class:`RedisSessionStore` — Azure Cache for Redis. Lowest-latency
  shared backend. Sessions are stored as JSON with a TTL slightly
  longer than the maximum expected call duration.
* :class:`CosmosSessionStore` — Azure Cosmos DB. Shared backend that
  reuses the Cosmos account the orchestrator already depends on, for
  regions where Azure Cache for Redis is unavailable/retired. Adds a
  few ms per read/write vs Redis — negligible on the webhook path.

The :func:`create_session_store` factory picks one based on the
``REDIS_URL`` / ``SESSION_COSMOS_CONTAINER`` environment variables, so
existing tests and local runs continue to work without configuration.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

from .models import CallSession

logger = logging.getLogger(__name__)

# Sessions in Redis are evicted after this many seconds. Long enough to
# cover the longest plausible call (a few minutes of conversation plus
# escalation handoff), short enough that orphaned state is reclaimed.
DEFAULT_SESSION_TTL_SECONDS = 3600


class SessionStore(Protocol):
    """Storage contract for active :class:`CallSession` objects."""

    def put(self, session: CallSession) -> None: ...
    def get(self, call_id: str) -> CallSession | None: ...
    def remove(self, call_id: str) -> CallSession | None: ...


class InMemorySessionStore:
    """Process-local session store. Default for local dev and tests."""

    def __init__(self) -> None:
        self._sessions: dict[str, CallSession] = {}

    def put(self, session: CallSession) -> None:
        self._sessions[session.call_id] = session

    def get(self, call_id: str) -> CallSession | None:
        return self._sessions.get(call_id)

    def remove(self, call_id: str) -> CallSession | None:
        return self._sessions.pop(call_id, None)


class RedisSessionStore:
    """Redis-backed session store for multi-replica production deployments.

    The ``redis`` package is imported lazily so that local environments
    without it (and unit tests) can still import this module.
    """

    def __init__(self, redis_url: str, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS):
        try:
            import redis as _redis
        except ImportError as exc:  # pragma: no cover - exercised in deployment only
            raise RuntimeError(
                "RedisSessionStore requires the 'redis' package. "
                "Install it via `pip install redis`."
            ) from exc

        self._client = _redis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl_seconds
        logger.info("RedisSessionStore initialized (ttl=%ds)", ttl_seconds)

    @staticmethod
    def _key(call_id: str) -> str:
        return f"callout:session:{call_id}"

    def put(self, session: CallSession) -> None:
        self._client.setex(
            self._key(session.call_id),
            self._ttl,
            session.model_dump_json(),
        )

    def get(self, call_id: str) -> CallSession | None:
        raw = self._client.get(self._key(call_id))
        if raw is None:
            return None
        return CallSession.model_validate_json(raw)

    def remove(self, call_id: str) -> CallSession | None:
        key = self._key(call_id)
        # Use a pipeline so GET + DELETE are atomic — prevents a webhook
        # on another replica from seeing the session disappear mid-handler.
        pipe = self._client.pipeline()
        pipe.get(key)
        pipe.delete(key)
        raw, _ = pipe.execute()
        if raw is None:
            return None
        return CallSession.model_validate_json(raw)


class CosmosSessionStore:
    """Cosmos-backed session store for multi-replica deployments.

    Used where Azure Cache for Redis is unavailable (e.g. regions where
    the classic SKU is retired). Reuses the orchestrator's existing
    Cosmos account; the container is expected to be partitioned by
    ``/id`` with a default TTL so orphaned sessions are reclaimed.
    ``azure-cosmos`` is imported lazily so local environments and unit
    tests can import this module without it.
    """

    def __init__(
        self,
        cosmos_endpoint: str,
        database_name: str = "callout",
        container_name: str = "call_sessions",
    ):
        try:
            from azure.cosmos import CosmosClient
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:  # pragma: no cover - exercised in deployment only
            raise RuntimeError(
                "CosmosSessionStore requires 'azure-cosmos' and 'azure-identity'."
            ) from exc

        credential = DefaultAzureCredential()
        self._client = CosmosClient(url=cosmos_endpoint, credential=credential)
        self._container = (
            self._client.get_database_client(database_name)
            .get_container_client(container_name)
        )
        logger.info(
            "CosmosSessionStore initialized (database=%s container=%s)",
            database_name,
            container_name,
        )

    def put(self, session: CallSession) -> None:
        doc = session.model_dump(mode="json")
        doc["id"] = session.call_id
        self._container.upsert_item(body=doc)

    def get(self, call_id: str) -> CallSession | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            item = self._container.read_item(item=call_id, partition_key=call_id)
        except CosmosResourceNotFoundError:
            return None
        # Strip Cosmos system properties; pydantic ignores the extra "id".
        return CallSession.model_validate(
            {k: v for k, v in item.items() if not k.startswith("_")}
        )

    def remove(self, call_id: str) -> CallSession | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        session = self.get(call_id)
        if session is None:
            return None
        try:
            self._container.delete_item(item=call_id, partition_key=call_id)
        except CosmosResourceNotFoundError:
            pass
        return session


def create_session_store(
    redis_url: str | None = None,
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    cosmos_endpoint: str | None = None,
    cosmos_container: str | None = None,
) -> SessionStore:
    """Factory that returns the right backend based on configuration.

    Precedence:
    1. Explicit ``redis_url`` argument / ``REDIS_URL`` env var (lowest latency)
    2. ``SESSION_COSMOS_CONTAINER`` (+ ``COSMOS_ENDPOINT``) — Cosmos-backed,
       for regions without Redis
    3. Fallback to :class:`InMemorySessionStore`
    """
    url = redis_url or os.environ.get("REDIS_URL", "")
    if url:
        logger.info("Using RedisSessionStore (multi-replica safe)")
        return RedisSessionStore(url, ttl_seconds=ttl_seconds)

    container = cosmos_container or os.environ.get("SESSION_COSMOS_CONTAINER", "")
    endpoint = cosmos_endpoint or os.environ.get("COSMOS_ENDPOINT", "")
    if container and endpoint:
        logger.info("Using CosmosSessionStore (multi-replica safe)")
        return CosmosSessionStore(cosmos_endpoint=endpoint, container_name=container)

    logger.info(
        "Using InMemorySessionStore — SAFE ONLY WITH A SINGLE REPLICA. "
        "Set REDIS_URL or SESSION_COSMOS_CONTAINER to enable horizontal scale."
    )
    return InMemorySessionStore()
