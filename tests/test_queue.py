"""Tests for the optional Service Bus queue layer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.queue import _NullQueueClient, create_queue_client


def test_create_queue_client_returns_null_when_unconfigured():
    client = create_queue_client(namespace="", queue_name="")
    assert isinstance(client, _NullQueueClient)
    assert client.enabled is False


def test_create_queue_client_returns_null_when_namespace_missing():
    client = create_queue_client(namespace="", queue_name="alarm-intake")
    assert isinstance(client, _NullQueueClient)
    assert client.enabled is False


def test_create_queue_client_returns_null_when_queue_name_missing():
    client = create_queue_client(namespace="my-ns", queue_name="")
    assert isinstance(client, _NullQueueClient)
    assert client.enabled is False
