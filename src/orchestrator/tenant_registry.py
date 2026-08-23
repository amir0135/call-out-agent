"""Tenant registry — multi-customer/site/contact data backed by Cosmos DB.

Containers (database = callout-db):
  - customers       (pk = /customer_id)   one doc per customer
  - sites           (pk = /customer_id)   one doc per site
  - sop_overlays    (pk = /customer_id)   per-customer SOP patches

Falls back to local JSON (sops/contacts.json + sops/customers.json) when
COSMOS_ENDPOINT is unset (LOCAL_MODE / unit tests).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_LOCAL_CONTACTS = _REPO_ROOT / "sops" / "contacts.json"
_LOCAL_CUSTOMERS = _REPO_ROOT / "sops" / "customers.json"
_LOCAL_OVERLAYS = _REPO_ROOT / "sops" / "overlays"


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------
class Contact:
    def __init__(self, data: dict[str, Any]):
        self.name: str = data["name"]
        self.phone_number: str = data["phone_number"]
        self.role: str = data.get("role", "")
        self.priority: int = int(data.get("priority", 99))
        self.stores: list[str] = data.get("stores", [])
        self.call_window_start: str | None = data.get("call_window_start")
        self.call_window_end: str | None = data.get("call_window_end")

    def matches_store(self, store_id: str | None) -> bool:
        if not self.stores:
            return True
        return store_id in self.stores

    def in_call_window(self, now: datetime | None = None) -> bool:
        if not self.call_window_start or not self.call_window_end:
            return True
        now = now or datetime.now()
        start = time.fromisoformat(self.call_window_start)
        end = time.fromisoformat(self.call_window_end)
        return start <= now.time() <= end


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
class _LocalBackend:
    """JSON-file backend used when no Cosmos endpoint is configured."""

    def __init__(self) -> None:
        self._customers: dict[str, dict] = {}
        self._contacts: dict[str, dict] = {}
        self._overlays: dict[tuple[str, str], list[dict]] = {}

    def load(self) -> None:
        if _LOCAL_CUSTOMERS.exists():
            with open(_LOCAL_CUSTOMERS, encoding="utf-8") as f:
                for c in json.load(f):
                    self._customers[c["customer_id"]] = c
        if _LOCAL_CONTACTS.exists():
            with open(_LOCAL_CONTACTS, encoding="utf-8") as f:
                self._contacts = json.load(f)
        if _LOCAL_OVERLAYS.exists() and _LOCAL_OVERLAYS.is_dir():
            for path in _LOCAL_OVERLAYS.glob("*.json"):
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
                self._overlays[(doc["customer_id"], doc["sop_id"])] = doc.get("patches", [])
        logger.info(
            "Local registry loaded: %d customers, %d contact groups, %d overlays",
            len(self._customers),
            len(self._contacts),
            len(self._overlays),
        )

    def get_customer(self, customer_id: str) -> dict | None:
        return self._customers.get(customer_id)

    def list_customers(self) -> list[dict]:
        return list(self._customers.values())

    def get_contact_group(self, sop_id: str, customer_id: str | None) -> dict:
        # Customer-scoped key: f"{customer_id}:{sop_id}", then customer alone, then sop, then default
        keys = []
        if customer_id:
            keys += [f"{customer_id}:{sop_id}", customer_id]
        keys += [sop_id, "default"]
        for k in keys:
            if k in self._contacts:
                return self._contacts[k]
        return {}

    def get_overlay(self, customer_id: str, sop_id: str) -> list[dict]:
        return self._overlays.get((customer_id, sop_id), [])


class _CosmosBackend:
    """Cosmos DB backend (production)."""

    def __init__(self, endpoint: str, database: str = "callout") -> None:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        self._client = CosmosClient(endpoint, credential=DefaultAzureCredential())
        self._db = self._client.get_database_client(database)
        self._customers = self._db.get_container_client("customers")
        self._sites = self._db.get_container_client("sites")
        self._contacts = self._db.get_container_client("contacts")
        self._overlays = self._db.get_container_client("sop_overlays")

    def load(self) -> None:
        # Cosmos is queried lazily per call; nothing to preload.
        return

    def get_customer(self, customer_id: str) -> dict | None:
        try:
            return self._customers.read_item(item=customer_id, partition_key=customer_id)
        except Exception:
            return None

    def list_customers(self) -> list[dict]:
        return list(self._customers.read_all_items())

    def get_contact_group(self, sop_id: str, customer_id: str | None) -> dict:
        # Try most specific → least specific
        candidates = []
        if customer_id:
            candidates.append((f"{customer_id}:{sop_id}", customer_id))
            candidates.append((customer_id, customer_id))
        candidates.append((sop_id, "_global"))
        candidates.append(("default", "_global"))
        for item_id, pk in candidates:
            try:
                return self._contacts.read_item(item=item_id, partition_key=pk)
            except Exception:
                continue
        return {}

    def get_overlay(self, customer_id: str, sop_id: str) -> list[dict]:
        try:
            doc = self._overlays.read_item(
                item=f"{customer_id}:{sop_id}",
                partition_key=customer_id,
            )
            return doc.get("patches", [])
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Public registry façade
# ---------------------------------------------------------------------------
class TenantRegistry:
    """Single entry-point: customer info, contact resolution, SOP overlays."""

    def __init__(self, cosmos_endpoint: str | None = None) -> None:
        endpoint = cosmos_endpoint if cosmos_endpoint is not None else os.environ.get("COSMOS_ENDPOINT", "")
        if endpoint:
            try:
                self._backend: _LocalBackend | _CosmosBackend = _CosmosBackend(endpoint)
                logger.info("TenantRegistry using Cosmos backend at %s", endpoint)
            except Exception:
                logger.exception("Cosmos backend init failed; falling back to local files")
                self._backend = _LocalBackend()
        else:
            self._backend = _LocalBackend()
        self._backend.load()

    # -- Customers ------------------------------------------------------
    def get_customer(self, customer_id: str) -> dict | None:
        return self._backend.get_customer(customer_id)

    def list_customers(self) -> list[dict]:
        return self._backend.list_customers()

    # -- Contacts -------------------------------------------------------
    def on_call(self, sop_id: str, customer_id: str | None, store_id: str | None = None) -> list[Contact]:
        group = self._backend.get_contact_group(sop_id, customer_id)
        raw = group.get("on_call_technicians", [])
        contacts = [Contact(c) for c in raw if isinstance(c, dict)]
        contacts = [c for c in contacts if c.matches_store(store_id)]
        contacts.sort(key=lambda c: c.priority)
        return contacts

    def escalation(self, sop_id: str, customer_id: str | None) -> list[Contact]:
        group = self._backend.get_contact_group(sop_id, customer_id)
        raw = group.get("escalation", [])
        contacts = [Contact(c) for c in raw if isinstance(c, dict)]
        contacts.sort(key=lambda c: c.priority)
        return contacts

    def resolve_primary(
        self,
        sop_id: str,
        customer_id: str | None = None,
        store_id: str | None = None,
    ) -> Contact | None:
        for contact in self.on_call(sop_id, customer_id, store_id):
            if contact.in_call_window():
                return contact
        return None

    # -- SOP overlays ---------------------------------------------------
    def get_overlay(self, customer_id: str, sop_id: str) -> list[dict]:
        """Return list of JSON-Patch operations to apply to the base SOP."""
        return self._backend.get_overlay(customer_id, sop_id)
