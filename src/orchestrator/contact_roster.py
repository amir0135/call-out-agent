"""Contact roster — resolves which phone numbers to call for a given SOP/alarm."""

from __future__ import annotations

import json
import logging
from datetime import datetime, time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_ROSTER_PATH = Path(__file__).resolve().parent.parent.parent / "sops" / "contacts.json"


class Contact:
    """A single contact entry."""

    def __init__(self, data: dict[str, Any]):
        self.name: str = data["name"]
        self.phone_number: str = data["phone_number"]
        self.role: str = data.get("role", "")
        self.priority: int = int(data.get("priority", 99))
        self.stores: list[str] = data.get("stores", [])
        self.call_window_start: str | None = data.get("call_window_start")
        self.call_window_end: str | None = data.get("call_window_end")

    def matches_store(self, store_id: str | None) -> bool:
        """Return True if this contact covers the given store (or covers all)."""
        if not self.stores:
            return True
        return store_id in self.stores

    def in_call_window(self, now: datetime | None = None) -> bool:
        """Return True if the current local time is inside the contact's call window."""
        if not self.call_window_start or not self.call_window_end:
            return True
        now = now or datetime.now()
        start = time.fromisoformat(self.call_window_start)
        end = time.fromisoformat(self.call_window_end)
        return start <= now.time() <= end

    def __repr__(self) -> str:
        return f"Contact(name={self.name!r}, phone={self.phone_number}, role={self.role})"


class ContactRoster:
    """Loads the contact roster and resolves contacts by SOP + context."""

    def __init__(self, roster_path: str | Path | None = None):
        self._path = Path(roster_path) if roster_path else _DEFAULT_ROSTER_PATH
        self._data: dict[str, Any] = {}

    def load(self) -> None:
        with open(self._path) as f:
            self._data = json.load(f)
        logger.info("Loaded contact roster from %s", self._path)

    def _group(self, sop_id: str) -> dict[str, Any]:
        """Return the SOP-specific group or fall back to 'default'."""
        if not self._data:
            self.load()
        return self._data.get(sop_id) or self._data.get("default") or {}

    def on_call(self, sop_id: str, store_id: str | None = None) -> list[Contact]:
        """Return ordered list of on-call technicians for this SOP/store."""
        group = self._group(sop_id)
        raw = group.get("on_call_technicians", [])
        contacts = [Contact(c) for c in raw if isinstance(c, dict)]
        contacts = [c for c in contacts if c.matches_store(store_id)]
        contacts.sort(key=lambda c: c.priority)
        return contacts

    def escalation(self, sop_id: str) -> list[Contact]:
        """Return ordered list of escalation contacts for this SOP."""
        group = self._group(sop_id)
        raw = group.get("escalation", [])
        contacts = [Contact(c) for c in raw if isinstance(c, dict)]
        contacts.sort(key=lambda c: c.priority)
        return contacts

    def resolve_primary(self, sop_id: str, store_id: str | None = None) -> Contact | None:
        """Pick the highest-priority on-call contact currently in their call window."""
        for contact in self.on_call(sop_id, store_id):
            if contact.in_call_window():
                return contact
        return None
