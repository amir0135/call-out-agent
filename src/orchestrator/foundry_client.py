"""HTTP client for invoking the Foundry hosted SOP reasoning agent."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import AgentRequest, AgentResponse

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15.0  # seconds
_MAX_RETRIES = 2


class FoundryClient:
    """Invokes the Foundry hosted agent for SOP step reasoning.

    The agent receives alarm context, SOP definition, conversation history,
    and current step — then returns the next step, utterance, and reasoning.
    """

    def __init__(self, agent_endpoint: str, timeout: float = _DEFAULT_TIMEOUT):
        self._endpoint = agent_endpoint.rstrip("/")
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=self._timeout)
        self._consecutive_failures = 0
        self._circuit_open = False
        self._circuit_threshold = 3

    async def reason(self, request: AgentRequest) -> AgentResponse:
        """Send a reasoning request to the Foundry agent.

        Includes retry logic and a simple circuit breaker.
        """
        if self._circuit_open:
            logger.error("Circuit breaker open — returning escalation response")
            return AgentResponse(
                next_step="escalate_unclear",
                utterance="I apologize, but I'm experiencing a technical issue. Let me connect you with a human operator.",
                reasoning="Circuit breaker open due to consecutive failures",
                should_escalate=True,
                outcome="escalate_human",
            )

        url = f"{self._endpoint}/api/reason"
        payload = request.model_dump()

        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = await self._client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()

                self._consecutive_failures = 0
                self._circuit_open = False

                return AgentResponse(**data)

            except httpx.TimeoutException as exc:
                last_error = exc
                logger.warning(
                    "Foundry agent timeout (attempt %d/%d): %s",
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )
            except httpx.HTTPStatusError as exc:
                last_error = exc
                logger.warning(
                    "Foundry agent HTTP error %d (attempt %d/%d)",
                    exc.response.status_code,
                    attempt,
                    _MAX_RETRIES,
                )
            except Exception as exc:
                last_error = exc
                logger.exception("Unexpected error calling Foundry agent")
                break

        # All retries exhausted
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._circuit_threshold:
            self._circuit_open = True
            logger.error("Circuit breaker tripped after %d consecutive failures", self._consecutive_failures)

        logger.error("Foundry agent call failed after %d attempts: %s", _MAX_RETRIES, last_error)
        return AgentResponse(
            next_step="escalate_unclear",
            utterance="I apologize, but I'm experiencing a technical issue. Let me connect you with a human operator.",
            reasoning=f"Agent call failed: {last_error}",
            should_escalate=True,
            outcome="escalate_human",
        )

    async def health_check(self) -> bool:
        """Check if the Foundry agent is reachable."""
        try:
            response = await self._client.get(f"{self._endpoint}/health")
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
