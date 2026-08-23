"""Outbound outcome webhook — pushes terminal call results to customer tools.

Integration surface for the alarm system / ticketing / monitoring stack:
when a call reaches a terminal state (resolved, escalated, no-answer,
failed) the orchestrator POSTs a JSON summary to a configurable webhook
so downstream tools do not have to poll.

Configuration (all optional — unset ⇒ no-op, existing behaviour):

* ``OUTCOME_WEBHOOK_URL``    — default destination for all call results.
* ``OUTCOME_WEBHOOK_SECRET`` — when set, each POST carries an
  ``X-Callout-Signature`` header: hex HMAC-SHA256 of the raw body.
  Receivers verify it to authenticate the sender (OWASP: never trust
  an unauthenticated webhook).

A :class:`~orchestrator.models.CallIntent` can also carry a per-alarm
``callback_url`` which takes precedence over the env-level default, so
different upstream systems can route their own results.

Delivery is at-most-once per attempt with bounded retries (the audit
trail in Cosmos remains the source of truth; the webhook is a
convenience push). Failures never affect call handling.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging

import httpx

from .models import CallSession

logger = logging.getLogger(__name__)

# Bounded retry schedule (seconds between attempts). Short + finite:
# the receiver can always reconcile from GET /api/alarm-status or Cosmos.
RETRY_DELAYS = (1.0, 5.0, 30.0)
REQUEST_TIMEOUT_SECONDS = 10.0


def build_outcome_payload(session: CallSession) -> dict:
    """Serialize a terminal call session into the webhook payload."""
    intent = session.intent
    return {
        "event": "call_completed",
        "intent_id": intent.intent_id,
        "call_id": session.call_id,
        "alarm_id": intent.alarm.alarm_id,
        "alarm_type": intent.alarm.alarm_type,
        "customer_id": intent.resolved_customer_id,
        "store_id": intent.alarm.store_id,
        "sop_id": intent.sop_id,
        "outcome": session.outcome.value if session.outcome else None,
        "attempt": session.attempt_number,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "final_step": session.current_step,
        "recording_url": session.recording_url,
        "transcript": session.conversation_history,
    }


def sign_payload(body: bytes, secret: str) -> str:
    """Hex HMAC-SHA256 of the raw request body."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class OutcomeNotifier:
    """Fire-and-forget webhook publisher with bounded retries."""

    def __init__(self, webhook_url: str = "", secret: str = "", transport: httpx.AsyncBaseTransport | None = None):
        self._webhook_url = webhook_url
        self._secret = secret
        self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, transport=transport)
        if webhook_url:
            logger.info("OutcomeNotifier enabled (url=%s, signed=%s)", webhook_url, bool(secret))

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    def _resolve_url(self, session: CallSession) -> str:
        return session.intent.callback_url or self._webhook_url

    def notify(self, session: CallSession) -> None:
        """Schedule delivery in the background. Never raises."""
        url = self._resolve_url(session)
        if not url:
            return
        asyncio.get_running_loop().create_task(self._deliver(session, url))

    async def _deliver(self, session: CallSession, url: str) -> None:
        import json

        payload = build_outcome_payload(session)
        # Serialize once so the signature matches the exact bytes sent.
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers["X-Callout-Signature"] = sign_payload(body, self._secret)

        for attempt, delay in enumerate((0.0,) + RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self._client.post(url, content=body, headers=headers)
                if response.status_code < 400:
                    logger.info(
                        "Outcome webhook delivered for call %s (alarm %s) → %s",
                        session.call_id,
                        session.intent.alarm.alarm_id,
                        response.status_code,
                    )
                    return
                logger.warning(
                    "Outcome webhook attempt %d for call %s returned %s",
                    attempt + 1,
                    session.call_id,
                    response.status_code,
                )
            except Exception as exc:
                logger.warning(
                    "Outcome webhook attempt %d for call %s failed: %s",
                    attempt + 1,
                    session.call_id,
                    exc,
                )
        logger.error(
            "Outcome webhook delivery FAILED after %d attempts for call %s — "
            "receiver should reconcile via GET /api/alarm-status/{alarm_id}",
            len(RETRY_DELAYS) + 1,
            session.call_id,
        )

    async def close(self) -> None:
        await self._client.aclose()
