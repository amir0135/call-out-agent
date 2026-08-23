"""Tests for the outcome webhook notifier (push integration surface)."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from orchestrator.models import AlarmContext, CallIntent, CallOutcome, CallSession
from orchestrator.outcome_notifier import (
    OutcomeNotifier,
    build_outcome_payload,
    sign_payload,
)


def _make_session(callback_url: str | None = None) -> CallSession:
    alarm = AlarmContext(
        alarm_id="ALM-WH-1",
        alarm_type="high_temperature",
        store_name="Store 1",
        store_id="ST-1",
        equipment_name="Freezer A",
        current_temp=15.0,
        threshold_temp=8.0,
        alarm_time="2026-07-08T10:00:00Z",
        customer_id="CUST-1",
    )
    intent = CallIntent(
        intent_id="INT-WH-1",
        alarm=alarm,
        phone_number="+15555550100",
        callback_url=callback_url,
    )
    session = CallSession(call_id="CALL-WH-1", intent=intent)
    session.outcome = CallOutcome.RESOLVED
    session.ended_at = "2026-07-08T10:03:00Z"
    session.conversation_history = [
        {"role": "agent", "text": "Hello"},
        {"role": "callee", "text": "Yes, handled"},
    ]
    return session


class TestPayload:
    def test_payload_contains_integration_keys(self):
        payload = build_outcome_payload(_make_session())
        assert payload["event"] == "call_completed"
        assert payload["intent_id"] == "INT-WH-1"
        assert payload["alarm_id"] == "ALM-WH-1"
        assert payload["call_id"] == "CALL-WH-1"
        assert payload["outcome"] == "resolved"
        assert payload["customer_id"] == "CUST-1"
        assert payload["transcript"][-1]["text"] == "Yes, handled"

    def test_payload_outcome_none_safe(self):
        session = _make_session()
        session.outcome = None
        assert build_outcome_payload(session)["outcome"] is None


class TestSignature:
    def test_hmac_sha256_hex(self):
        body = b'{"a":1}'
        expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert sign_payload(body, "s3cret") == expected


class TestDelivery:
    async def test_delivers_signed_payload(self):
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = request.content
            captured["signature"] = request.headers.get("X-Callout-Signature")
            return httpx.Response(200)

        notifier = OutcomeNotifier(
            webhook_url="https://tools.example.com/callout-results",
            secret="s3cret",
            transport=httpx.MockTransport(handler),
        )
        session = _make_session()
        await notifier._deliver(session, notifier._resolve_url(session))

        assert captured["url"] == "https://tools.example.com/callout-results"
        payload = json.loads(captured["body"])
        assert payload["alarm_id"] == "ALM-WH-1"
        assert captured["signature"] == sign_payload(captured["body"], "s3cret")
        await notifier.close()

    async def test_per_intent_callback_url_wins(self):
        notifier = OutcomeNotifier(webhook_url="https://default.example.com/hook")
        session = _make_session(callback_url="https://tenant.example.com/hook")
        assert notifier._resolve_url(session) == "https://tenant.example.com/hook"
        await notifier.close()

    async def test_retries_then_succeeds(self, monkeypatch):
        import orchestrator.outcome_notifier as mod

        monkeypatch.setattr(mod, "RETRY_DELAYS", (0.0, 0.0, 0.0))
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(500 if calls["n"] < 3 else 200)

        notifier = OutcomeNotifier(
            webhook_url="https://tools.example.com/hook",
            transport=httpx.MockTransport(handler),
        )
        session = _make_session()
        await notifier._deliver(session, notifier._resolve_url(session))
        assert calls["n"] == 3
        await notifier.close()

    async def test_gives_up_after_bounded_retries(self, monkeypatch):
        import orchestrator.outcome_notifier as mod

        monkeypatch.setattr(mod, "RETRY_DELAYS", (0.0, 0.0, 0.0))
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503)

        notifier = OutcomeNotifier(
            webhook_url="https://tools.example.com/hook",
            transport=httpx.MockTransport(handler),
        )
        session = _make_session()
        # Must not raise — the audit trail is the source of truth.
        await notifier._deliver(session, notifier._resolve_url(session))
        assert calls["n"] == 4  # 1 initial + 3 retries
        await notifier.close()

    async def test_disabled_notifier_is_noop(self):
        notifier = OutcomeNotifier(webhook_url="")
        assert not notifier.enabled
        # notify() with no URL anywhere must be a silent no-op.
        notifier.notify(_make_session())
        await notifier.close()
