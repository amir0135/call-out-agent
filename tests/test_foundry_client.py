"""Tests for the Foundry Client — HTTP invocation with retry & circuit breaker."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.foundry_client import FoundryClient
from orchestrator.models import AgentRequest


@pytest.fixture
def agent_request():
    return AgentRequest(
        alarm_context={
            "alarm_id": "ALM-001",
            "alarm_type": "high_temperature",
            "severity": "high",
            "store_name": "Test Store",
            "store_id": "ST-001",
            "equipment_name": "Cooler Unit A",
            "current_temp": 12.5,
            "threshold_temp": 8.0,
            "alarm_time": "2026-04-16T10:30:00Z",
            "customer_id": "CUST-001",
        },
        sop_definition={"sop_id": "SOP-HTA-001", "steps": []},
        conversation_history=[
            {"role": "agent", "text": "Hello, this is Contoso."},
            {"role": "callee", "text": "Yes, I'm aware of the alarm."},
        ],
        current_step="ask_awareness",
    )


class TestFoundryClient:
    @pytest.mark.asyncio
    async def test_successful_reason_call(self, httpx_mock, agent_request):
        """Test successful agent invocation."""
        client = FoundryClient(agent_endpoint="http://localhost:8080")

        httpx_mock.add_response(
            url="http://localhost:8080/api/reason",
            json={
                "next_step": "ask_action_taken",
                "utterance": "Has any action been taken?",
                "reasoning": "Callee is aware, moving to next step",
                "should_escalate": False,
                "outcome": None,
            },
        )

        response = await client.reason(agent_request)
        assert response.next_step == "ask_action_taken"
        assert response.should_escalate is False
        await client.close()

    @pytest.mark.asyncio
    async def test_timeout_returns_escalation(self, httpx_mock, agent_request):
        """Test that timeout triggers safe escalation response."""
        client = FoundryClient(agent_endpoint="http://localhost:8080", timeout=0.1)

        httpx_mock.add_exception(httpx.TimeoutException("timeout"))
        httpx_mock.add_exception(httpx.TimeoutException("timeout"))

        response = await client.reason(agent_request)
        assert response.should_escalate is True
        assert response.outcome == "escalate_human"
        await client.close()

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips(self, httpx_mock, agent_request):
        """Test circuit breaker opens after consecutive failures."""
        client = FoundryClient(agent_endpoint="http://localhost:8080")
        client._circuit_threshold = 2

        # Fail twice to trip the breaker
        httpx_mock.add_exception(httpx.TimeoutException("timeout"))
        httpx_mock.add_exception(httpx.TimeoutException("timeout"))
        await client.reason(agent_request)

        httpx_mock.add_exception(httpx.TimeoutException("timeout"))
        httpx_mock.add_exception(httpx.TimeoutException("timeout"))
        await client.reason(agent_request)

        # Circuit should be open now — no HTTP call made
        response = await client.reason(agent_request)
        assert response.should_escalate is True
        assert "Circuit breaker" in response.reasoning
        await client.close()

    @pytest.mark.asyncio
    async def test_health_check(self, httpx_mock):
        """Test health check endpoint."""
        client = FoundryClient(agent_endpoint="http://localhost:8080")

        httpx_mock.add_response(url="http://localhost:8080/health", status_code=200)
        assert await client.health_check() is True

        await client.close()
