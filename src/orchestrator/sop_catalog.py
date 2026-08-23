"""SOP catalog — loads base SOPs and applies per-customer JSON-Patch overlays.

Base SOPs live in sops/base/ (or sops/ for backward compatibility) and are
shared across all customers. Customer-specific differences are stored as
RFC 6902 JSON Patch documents in the tenant registry (sop_overlays container).

The catalog caches resolved SOPs per (customer_id, sop_id, version).
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from .models import SOPDefinition

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SOP_BASE_DIRS = [_REPO_ROOT / "sops" / "base", _REPO_ROOT / "sops"]


def _apply_patches(doc: dict[str, Any], patches: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply RFC 6902 JSON Patch operations to a document.

    Supports the subset we actually use: replace, add, remove.
    External jsonpatch library would be ideal but we keep deps minimal.
    """
    result = copy.deepcopy(doc)
    for patch in patches:
        op = patch.get("op")
        path = patch.get("path", "")
        if not path.startswith("/"):
            logger.warning("Skipping patch with invalid path: %s", path)
            continue
        parts = [_unescape(p) for p in path.lstrip("/").split("/")]
        if op == "replace" or op == "add":
            _set_at(result, parts, patch.get("value"))
        elif op == "remove":
            _remove_at(result, parts)
        else:
            logger.warning("Unsupported patch op: %s", op)
    return result


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _set_at(doc: Any, parts: list[str], value: Any) -> None:
    cursor = doc
    for i, part in enumerate(parts[:-1]):
        key: Any = int(part) if isinstance(cursor, list) else part
        cursor = cursor[key]
    last = parts[-1]
    if isinstance(cursor, list):
        idx = len(cursor) if last == "-" else int(last)
        if idx == len(cursor):
            cursor.append(value)
        else:
            cursor[idx] = value
    else:
        cursor[last] = value


def _remove_at(doc: Any, parts: list[str]) -> None:
    cursor = doc
    for part in parts[:-1]:
        key: Any = int(part) if isinstance(cursor, list) else part
        cursor = cursor[key]
    last = parts[-1]
    key = int(last) if isinstance(cursor, list) else last
    del cursor[key]


class SOPCatalog:
    """Loads base SOPs once, resolves customer-specific variants on demand."""

    def __init__(self, registry: Any | None = None) -> None:
        # registry is a TenantRegistry — passed in to avoid circular import
        self._registry = registry
        self._base: dict[str, dict[str, Any]] = {}
        self._resolved_cache: dict[tuple[str, str], SOPDefinition] = {}

    def load(self) -> None:
        """Discover all base SOP files."""
        for base_dir in _SOP_BASE_DIRS:
            if not base_dir.exists():
                continue
            for path in base_dir.glob("*.json"):
                # skip non-SOP files in sops/
                if path.name in ("contacts.json", "customers.json"):
                    continue
                try:
                    with open(path, encoding="utf-8") as f:
                        doc = json.load(f)
                    if "sop_id" in doc and "steps" in doc:
                        self._base[doc["sop_id"]] = doc
                        logger.info(
                            "Loaded base SOP %s v%s from %s",
                            doc["sop_id"],
                            doc.get("version", "?"),
                            path.name,
                        )
                except Exception:
                    logger.exception("Failed to load SOP candidate %s", path)

    def list_base_sops(self) -> list[str]:
        return list(self._base.keys())

    def get_base(self, sop_id: str) -> dict[str, Any] | None:
        return self._base.get(sop_id)

    def resolve(self, sop_id: str, customer_id: str | None = None) -> SOPDefinition:
        """Return the SOP definition for the given customer, applying overlays."""
        cache_key = (customer_id or "_global", sop_id)
        if cache_key in self._resolved_cache:
            return self._resolved_cache[cache_key]

        base = self._base.get(sop_id)
        if base is None:
            raise ValueError(f"Unknown SOP: {sop_id}")

        merged = base
        if customer_id and self._registry is not None:
            patches = self._registry.get_overlay(customer_id, sop_id)
            if patches:
                logger.info(
                    "Applying %d overlay patches for customer=%s sop=%s",
                    len(patches),
                    customer_id,
                    sop_id,
                )
                merged = _apply_patches(base, patches)

        definition = SOPDefinition(**merged)
        self._resolved_cache[cache_key] = definition
        return definition

    def invalidate(self, customer_id: str | None = None, sop_id: str | None = None) -> None:
        """Clear cached resolved SOPs (call after overlays change)."""
        if customer_id is None and sop_id is None:
            self._resolved_cache.clear()
        else:
            self._resolved_cache = {
                k: v
                for k, v in self._resolved_cache.items()
                if (customer_id is not None and k[0] != (customer_id or "_global"))
                or (sop_id is not None and k[1] != sop_id)
            }
