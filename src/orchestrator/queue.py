"""Async Service Bus client for burst-resilient alarm ingestion.

Two scenarios:

* **No Service Bus configured** (``SERVICEBUS_NAMESPACE`` unset).
  Both :func:`enqueue_intent` and :func:`AlarmIntentConsumer` are no-ops
  so local dev, unit tests, and small-scale single-replica deployments
  continue to work synchronously.

* **Service Bus configured.** ``POST /api/alarm-intent`` enqueues the
  raw intent JSON and returns 202 immediately. The orchestrator (or a
  separate consumer container app) drains the queue and calls the
  existing processing pipeline. Combined with the KEDA
  ``azure-servicebus`` scale rule on Container Apps this lets the system
  absorb large alarm bursts (e.g. regional power outage → thousands of
  fridges all triggering at once) without blocking the alarm system's
  HTTP timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

from .models import CallIntent

logger = logging.getLogger(__name__)


class _NullQueueClient:
    """No-op queue used when Service Bus is not configured."""

    enabled = False

    async def enqueue_intent(self, intent: CallIntent) -> None:  # pragma: no cover - trivial
        raise RuntimeError("Queue not configured; caller should fall back to sync path.")

    async def close(self) -> None:  # pragma: no cover - trivial
        return None


class ServiceBusQueueClient:
    """Thin async wrapper around ``azure.servicebus.aio`` for alarm intents."""

    enabled = True

    def __init__(self, namespace: str, queue_name: str):
        # Imported lazily so the dependency is optional for local-only use.
        from azure.identity.aio import DefaultAzureCredential
        from azure.servicebus.aio import ServiceBusClient

        self._queue_name = queue_name
        self._credential = DefaultAzureCredential()
        self._client = ServiceBusClient(
            fully_qualified_namespace=f"{namespace}.servicebus.windows.net",
            credential=self._credential,
        )
        logger.info(
            "ServiceBusQueueClient initialized (namespace=%s queue=%s)",
            namespace,
            queue_name,
        )

    async def enqueue_intent(self, intent: CallIntent) -> None:
        from azure.servicebus import ServiceBusMessage

        payload = intent.model_dump_json()
        async with self._client.get_queue_sender(self._queue_name) as sender:
            await sender.send_messages(
                ServiceBusMessage(
                    body=payload,
                    content_type="application/json",
                    # Broker dedupe key — use intent_id so retried webhook
                    # submissions collapse into one message while distinct
                    # alarms for the same equipment still flow through.
                    message_id=intent.intent_id,
                    # Keep per-alarm ordering where sessions are enabled.
                    session_id=intent.alarm.alarm_id,
                )
            )
        logger.info(
            "Enqueued alarm %s to %s",
            intent.alarm.alarm_id,
            self._queue_name,
        )

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()


def create_queue_client(namespace: str, queue_name: str) -> _NullQueueClient | ServiceBusQueueClient:
    """Factory: return a real Service Bus client or a no-op."""
    if not namespace or not queue_name:
        logger.info(
            "Service Bus not configured (SERVICEBUS_NAMESPACE empty) \u2014 "
            "alarm intake will run synchronously. SAFE for low volumes; "
            "configure Service Bus + KEDA scaling for production bursts."
        )
        return _NullQueueClient()
    return ServiceBusQueueClient(namespace=namespace, queue_name=queue_name)


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------

ProcessIntent = Callable[[CallIntent], Awaitable[None]]


class AlarmIntentConsumer:
    """Background task that drains the alarm-intent queue.

    The consumer is started from the FastAPI ``lifespan`` and stopped on
    shutdown. It runs in the same process as the webhook handler; KEDA
    scales the number of orchestrator replicas based on queue depth so
    consumers fan out horizontally.
    """

    def __init__(
        self,
        namespace: str,
        queue_name: str,
        handler: ProcessIntent,
        max_concurrent: int = 5,
    ):
        from azure.identity.aio import DefaultAzureCredential
        from azure.servicebus.aio import ServiceBusClient

        self._queue_name = queue_name
        self._handler = handler
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._credential = DefaultAzureCredential()
        self._client = ServiceBusClient(
            fully_qualified_namespace=f"{namespace}.servicebus.windows.net",
            credential=self._credential,
        )
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="alarm-intent-consumer")
        logger.info(
            "AlarmIntentConsumer started (queue=%s, max_concurrent=%d)",
            self._queue_name,
            self._semaphore._value,  # initial permits
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                logger.warning("Consumer did not stop in 10s; cancelling")
                self._task.cancel()
        await self._client.close()
        await self._credential.close()

    async def _run(self) -> None:
        async with self._client.get_queue_receiver(
            queue_name=self._queue_name,
            max_wait_time=5,
        ) as receiver:
            while not self._stop_event.is_set():
                try:
                    messages = await receiver.receive_messages(
                        max_message_count=10,
                        max_wait_time=5,
                    )
                except Exception:
                    logger.exception("Receive loop error \u2014 backing off 5s")
                    await asyncio.sleep(5)
                    continue

                for msg in messages:
                    await self._semaphore.acquire()
                    asyncio.create_task(self._process_one(receiver, msg))

    async def _process_one(self, receiver, msg) -> None:
        try:
            body = b"".join(msg.body).decode("utf-8") if hasattr(msg.body, "__iter__") else str(msg)
            payload = json.loads(body)
            intent = CallIntent.model_validate(payload)
            await self._handler(intent)
            await receiver.complete_message(msg)
        except Exception:
            # Let Service Bus redeliver. After max_delivery_count it goes
            # to the dead-letter sub-queue for human inspection.
            logger.exception("Failed to process queued alarm intent; abandoning")
            try:
                await receiver.abandon_message(msg)
            except Exception:
                logger.exception("Failed to abandon message; will redeliver after lock expiry")
        finally:
            self._semaphore.release()
