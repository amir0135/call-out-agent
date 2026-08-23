"""Contract tests for the pluggable session and retry stores.

These tests document the behavioural contract every backend must
satisfy. The in-memory implementations are exercised directly here.
The Redis / Cosmos backends share the same surface and are validated
manually against live resources (see ``docs/multi-replica-rollout.md``)
since spinning them up in CI is out of scope for the PoC.
"""

from __future__ import annotations

import pytest

from orchestrator.models import AlarmContext, CallIntent, CallOutcome, CallSession
from orchestrator.retry_store import InMemoryRetryStore, RetryRecord
from orchestrator.session_store import InMemorySessionStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_session(call_id: str = "call-1") -> CallSession:
    alarm = AlarmContext(
        alarm_id="alarm-1",
        alarm_type="HIGH_TEMP",
        store_name="Store 1",
        store_id="s1",
        equipment_name="Freezer A",
        current_temp=-2.0,
        threshold_temp=-15.0,
        alarm_time="2025-01-01T00:00:00Z",
        customer_id="cust-1",
    )
    intent = CallIntent(alarm=alarm, phone_number="+15555550100")
    return CallSession(call_id=call_id, intent=intent)


# ---------------------------------------------------------------------------
# Session store contract
# ---------------------------------------------------------------------------


class TestInMemorySessionStoreContract:
    def test_put_then_get_returns_same_session(self):
        store = InMemorySessionStore()
        session = _make_session("call-A")
        store.put(session)
        assert store.get("call-A") is session

    def test_get_missing_returns_none(self):
        assert InMemorySessionStore().get("nonexistent") is None

    def test_remove_returns_and_evicts(self):
        store = InMemorySessionStore()
        session = _make_session("call-B")
        store.put(session)
        assert store.remove("call-B") is session
        assert store.get("call-B") is None

    def test_remove_missing_returns_none(self):
        assert InMemorySessionStore().remove("nonexistent") is None

    def test_put_overwrites_existing(self):
        store = InMemorySessionStore()
        first = _make_session("call-C")
        store.put(first)
        second = _make_session("call-C")
        second.current_step = "ask_severity"
        store.put(second)
        assert store.get("call-C") is second


# ---------------------------------------------------------------------------
# Retry store contract
# ---------------------------------------------------------------------------


class TestInMemoryRetryStoreContract:
    def test_get_missing_returns_none(self):
        assert InMemoryRetryStore().get("nonexistent") is None

    def test_upsert_then_get(self):
        store = InMemoryRetryStore()
        record = RetryRecord(alarm_id="alarm-X", max_retries=3)
        store.upsert(record)
        loaded = store.get("alarm-X")
        assert loaded is not None
        assert loaded.alarm_id == "alarm-X"
        assert loaded.attempt_count == 0

    def test_upsert_persists_attempts(self):
        store = InMemoryRetryStore()
        record = RetryRecord(alarm_id="alarm-Y", max_retries=3)
        record.record_attempt(CallOutcome.NO_ANSWER)
        store.upsert(record)
        loaded = store.get("alarm-Y")
        assert loaded is not None
        assert loaded.attempt_count == 1

    def test_delete_evicts(self):
        store = InMemoryRetryStore()
        record = RetryRecord(alarm_id="alarm-Z")
        store.upsert(record)
        store.delete("alarm-Z")
        assert store.get("alarm-Z") is None

    def test_delete_missing_is_noop(self):
        # Must not raise — matches Cosmos NotFound handling.
        InMemoryRetryStore().delete("nonexistent")


# ---------------------------------------------------------------------------
# RetryRecord serialization (used by CosmosRetryStore)
# ---------------------------------------------------------------------------


class TestRetryRecordSerialization:
    def test_to_dict_and_back_preserves_state(self):
        record = RetryRecord(alarm_id="alarm-S", max_retries=4)
        record.record_attempt(CallOutcome.NO_ANSWER)
        record.record_attempt(CallOutcome.RESOLVED)

        as_dict = record.to_dict()
        assert as_dict["id"] == "alarm-S"
        assert as_dict["alarmId"] == "alarm-S"
        assert as_dict["maxRetries"] == 4
        assert len(as_dict["attempts"]) == 2
        assert as_dict["finalOutcome"] == CallOutcome.RESOLVED.value

        round_tripped = RetryRecord.from_dict(as_dict)
        assert round_tripped.alarm_id == "alarm-S"
        assert round_tripped.max_retries == 4
        assert round_tripped.attempt_count == 2
        assert round_tripped.final_outcome == CallOutcome.RESOLVED

    def test_from_dict_captures_etag(self):
        record = RetryRecord.from_dict(
            {
                "alarmId": "alarm-E",
                "maxRetries": 3,
                "attempts": [],
                "finalOutcome": None,
                "_etag": "abc123",
            }
        )
        assert record._etag == "abc123"


# ---------------------------------------------------------------------------
# Factory selection: in-memory is the default when env vars are unset
# ---------------------------------------------------------------------------


def test_session_store_factory_defaults_to_in_memory(monkeypatch):
    from orchestrator.session_store import create_session_store

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("SESSION_COSMOS_CONTAINER", raising=False)
    monkeypatch.delenv("COSMOS_ENDPOINT", raising=False)
    store = create_session_store()
    assert isinstance(store, InMemorySessionStore)


def test_session_store_factory_selects_cosmos_without_redis(monkeypatch):
    """Cosmos backend engages when Redis is absent — horizontal scale in
    regions where Azure Cache for Redis is unavailable."""
    import orchestrator.session_store as mod

    created = {}

    class _StubCosmosStore:
        def __init__(self, cosmos_endpoint, container_name="call_sessions", **kwargs):
            created["endpoint"] = cosmos_endpoint
            created["container"] = container_name

    monkeypatch.setattr(mod, "CosmosSessionStore", _StubCosmosStore)
    monkeypatch.delenv("REDIS_URL", raising=False)
    store = mod.create_session_store(
        redis_url="",
        cosmos_endpoint="https://cosmos.example.com:443/",
        cosmos_container="call_sessions",
    )
    assert isinstance(store, _StubCosmosStore)
    assert created == {
        "endpoint": "https://cosmos.example.com:443/",
        "container": "call_sessions",
    }


def test_session_store_factory_redis_takes_precedence_over_cosmos(monkeypatch):
    """When both are configured, Redis (lowest latency) wins."""
    import orchestrator.session_store as mod

    class _StubRedisStore:
        def __init__(self, url, ttl_seconds=0):
            self.url = url

    monkeypatch.setattr(mod, "RedisSessionStore", _StubRedisStore)
    store = mod.create_session_store(
        redis_url="rediss://example:6380/0",
        cosmos_endpoint="https://cosmos.example.com:443/",
        cosmos_container="call_sessions",
    )
    assert isinstance(store, _StubRedisStore)


def test_retry_store_factory_defaults_to_in_memory(monkeypatch):
    from orchestrator.retry_store import create_retry_store

    monkeypatch.delenv("RETRY_STATE_COSMOS_ENDPOINT", raising=False)
    monkeypatch.delenv("RETRY_STATE_COSMOS_CONTAINER", raising=False)
    monkeypatch.delenv("COSMOS_ENDPOINT", raising=False)
    store = create_retry_store()
    assert isinstance(store, InMemoryRetryStore)


# ---------------------------------------------------------------------------
# RetryManager + RetryStore integration
# ---------------------------------------------------------------------------


def test_retry_manager_uses_injected_store():
    """RetryManager must delegate to the supplied store, not its own dict."""
    from orchestrator.retry_manager import RetryManager

    store = InMemoryRetryStore()
    manager = RetryManager(max_retries=3, store=store)
    manager.record_outcome("alarm-Q", CallOutcome.NO_ANSWER)

    # Store sees the same record the manager just mutated.
    record = store.get("alarm-Q")
    assert record is not None
    assert record.attempt_count == 1


def test_call_handler_uses_injected_session_store():
    """CallHandler must delegate session storage to the supplied store."""
    from orchestrator.call_handler import CallHandler

    store = InMemorySessionStore()
    handler = CallHandler(
        acs_endpoint="https://example.com",
        acs_phone_number="+15555550000",
        callback_base_url="https://example.com",
        speech_region="eastus",
        speech_voice="en-US-JennyNeural",
        cognitive_services_endpoint="https://example.com",
        acs_connection_string="endpoint=https://example.com;accesskey=fake",
        session_store=store,
    )
    session = _make_session("call-handler-test")
    handler.register_session(session)
    assert store.get("call-handler-test") is session
    assert handler.get_session("call-handler-test") is session
