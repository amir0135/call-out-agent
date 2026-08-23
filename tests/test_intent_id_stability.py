from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.app import _ensure_intent_id
from orchestrator.models import AlarmContext, CallIntent


def _make_intent(intent_id: str = "") -> CallIntent:
    alarm = AlarmContext(
        alarm_id="ALM-STABLE-001",
        alarm_type="high_temperature",
        severity="high",
        store_name="Store A",
        store_id="ST-001",
        equipment_name="Cooler 1",
        current_temp=12.3,
        threshold_temp=8.0,
        alarm_time="2026-05-23T09:00:00Z",
        customer_id="CUST-001",
    )
    return CallIntent(intent_id=intent_id, alarm=alarm, phone_number="+15550001111")


def test_ensure_intent_id_preserves_existing_id() -> None:
    intent = _make_intent(intent_id="external-intent-123")
    assert _ensure_intent_id(intent) == "external-intent-123"


def test_ensure_intent_id_is_deterministic_for_same_alarm() -> None:
    intent_a = _make_intent()
    intent_b = _make_intent()
    assert _ensure_intent_id(intent_a) == _ensure_intent_id(intent_b)
