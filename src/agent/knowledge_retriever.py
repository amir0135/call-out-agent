"""Knowledge retriever for agentic RAG across multiple Contoso domains.

Queries Azure AI Search indexes to retrieve domain-specific context that
the SOP agent uses to make better decisions during alarm verification calls.

Domains:
  - equipment: Technical specs, troubleshooting, operating ranges
  - maintenance: Recommended actions, repair guides, spare parts
  - store_profiles: Store contacts, equipment inventory, service history
  - alarm_history: Past alarms, patterns, recurring issues, resolutions

In LOCAL_MODE, returns static sample documents so development works
without an Azure AI Search resource.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

LOCAL_MODE = os.environ.get("LOCAL_MODE", "false").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AI_SEARCH_ENDPOINT = os.environ.get("AZURE_AI_SEARCH_ENDPOINT", "")
AI_SEARCH_API_VERSION = os.environ.get("AZURE_AI_SEARCH_API_VERSION", "2024-07-01")

# Index names per domain
INDEX_MAP: dict[str, str] = {
    "equipment": os.environ.get("INDEX_EQUIPMENT", "idx-equipment-manuals"),
    "maintenance": os.environ.get("INDEX_MAINTENANCE", "idx-maintenance-guides"),
    "store_profiles": os.environ.get("INDEX_STORE_PROFILES", "idx-store-profiles"),
    "alarm_history": os.environ.get("INDEX_ALARM_HISTORY", "idx-alarm-history"),
}

VALID_DOMAINS = set(INDEX_MAP.keys())


@dataclass
class RetrievedDoc:
    """A single document chunk returned from a knowledge base."""
    domain: str
    content: str
    title: str = ""
    score: float = 0.0
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Local mode — static sample documents for development
# ---------------------------------------------------------------------------
_LOCAL_KB: dict[str, list[dict]] = {
    "equipment": [
        {
            "title": "Contoso RC-550 Case Controller — Operating Manual",
            "content": (
                "The RC-550 supports temperature range -50°C to +50°C. "
                "Normal operating range for medium-temp cases: 2°C to 8°C. "
                "High temperature alarm triggers when reading exceeds setpoint + differential "
                "for longer than the alarm delay (default 30 min). "
                "Common causes: door left open, defrost cycle overrun, compressor failure, "
                "dirty condenser coil, refrigerant leak. "
                "First response: check door seals, verify defrost schedule, "
                "inspect compressor run status via controller display."
            ),
        },
        {
            "title": "Contoso RCU-100 Condensing Unit — Troubleshooting",
            "content": (
                "If discharge pressure is high: clean condenser coil, check fan motor. "
                "If suction pressure is low: check for refrigerant leak, inspect expansion valve. "
                "Unit not starting: verify power supply, check contactor, inspect overload relay. "
                "Compressor short-cycling: check high-pressure cutout, verify charge level. "
                "Noise/vibration: check mounting bolts, inspect compressor internals."
            ),
        },
    ],
    "maintenance": [
        {
            "title": "High Temperature Alarm — Recommended Actions",
            "content": (
                "Step 1: Verify alarm is genuine (not sensor drift). "
                "Step 2: Check if defrost cycle is active — wait for cycle to complete. "
                "Step 3: Inspect door seals and ensure doors are fully closed. "
                "Step 4: Check evaporator fan operation. "
                "Step 5: If temperature continues rising, check compressor operation. "
                "Step 6: If compressor is off, check power supply and controller settings. "
                "Step 7: If issue persists >2 hours, dispatch technician. "
                "Priority: MEDIUM if temp <15°C, HIGH if temp >15°C, CRITICAL if temp >25°C."
            ),
        },
        {
            "title": "Preventive Maintenance Schedule — Refrigeration",
            "content": (
                "Weekly: Check display temperatures, verify no active alarms. "
                "Monthly: Clean condenser coils, check door gaskets, verify defrost drain. "
                "Quarterly: Check refrigerant charge, inspect electrical connections, "
                "calibrate temperature sensors. "
                "Annually: Full system inspection, compressor oil analysis, "
                "replace air filters, test safety devices."
            ),
        },
    ],
    "store_profiles": [
        {
            "title": "Store Profile — Default Template",
            "content": (
                "Store configuration includes: number of refrigerated cases, "
                "equipment model list, primary and secondary contact numbers, "
                "service contract level (Basic/Premium/Enterprise), "
                "preferred service provider, operating hours, "
                "number of prior alarm calls this month. "
                "Premium stores get direct technician dispatch after 1 failed call. "
                "Basic stores escalate to Bugle after 3 call attempts."
            ),
        },
    ],
    "alarm_history": [
        {
            "title": "Alarm Pattern Analysis — High Temperature",
            "content": (
                "Most common root causes from last 12 months: "
                "1. Door left open (42%) — usually resolved by store staff. "
                "2. Defrost overrun (23%) — resolves automatically within 45 min. "
                "3. Compressor failure (15%) — requires technician dispatch. "
                "4. Dirty condenser (11%) — store can clean with guidance. "
                "5. Sensor drift (9%) — requires recalibration. "
                "Repeat alarms within 24h on same equipment: 78% indicate "
                "underlying mechanical issue requiring technician visit."
            ),
        },
    ],
}


def _search_local(domain: str, query: str, top_k: int = 3) -> list[RetrievedDoc]:
    """Return static local documents for a domain (no Azure needed)."""
    docs = _LOCAL_KB.get(domain, [])
    return [
        RetrievedDoc(
            domain=domain,
            title=d["title"],
            content=d["content"],
            score=1.0 - i * 0.1,
            metadata={"source": "local_kb"},
        )
        for i, d in enumerate(docs[:top_k])
    ]


# ---------------------------------------------------------------------------
# Azure AI Search — production retrieval
# ---------------------------------------------------------------------------
async def _search_azure(
    domain: str,
    query: str,
    top_k: int = 3,
    customer_id: str | None = None,
) -> list[RetrievedDoc]:
    """Query Azure AI Search with hybrid (keyword + vector) search."""
    import httpx
    from azure.identity import DefaultAzureCredential

    index_name = INDEX_MAP.get(domain)
    if not index_name:
        logger.warning("No index configured for domain '%s'", domain)
        return []

    credential = DefaultAzureCredential()
    token = credential.get_token("https://search.azure.com/.default")

    url = f"{AI_SEARCH_ENDPOINT}/indexes/{index_name}/docs/search?api-version={AI_SEARCH_API_VERSION}"

    body: dict = {
        "search": query,
        "queryType": "semantic",
        "semanticConfiguration": f"{index_name}-semantic",
        "top": top_k,
        "select": "title,content,metadata",
    }
    if customer_id:
        # Multi-tenant security filter: every doc must be tagged with customer_id
        body["filter"] = f"customer_id eq '{customer_id}'"

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {token.token}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()

    results = []
    for doc in data.get("value", []):
        results.append(
            RetrievedDoc(
                domain=domain,
                title=doc.get("title", ""),
                content=doc.get("content", ""),
                score=doc.get("@search.score", 0.0),
                metadata=doc.get("metadata", {}),
            )
        )
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def retrieve(
    domain: str,
    query: str,
    top_k: int = 3,
    customer_id: str | None = None,
) -> list[RetrievedDoc]:
    """Retrieve documents from a knowledge domain.

    Args:
        domain: One of 'equipment', 'maintenance', 'store_profiles', 'alarm_history'
        query: Natural language search query
        top_k: Number of results to return
        customer_id: Tenant scope. When set, only docs tagged with this
            customer_id are returned (multi-tenant security filter).

    Returns:
        List of retrieved document chunks
    """
    if domain not in VALID_DOMAINS:
        logger.warning("Invalid domain '%s'. Valid: %s", domain, VALID_DOMAINS)
        return []

    if LOCAL_MODE or not AI_SEARCH_ENDPOINT:
        return _search_local(domain, query, top_k)

    try:
        return await _search_azure(domain, query, top_k, customer_id=customer_id)
    except Exception:
        logger.exception("Azure AI Search query failed for domain '%s'", domain)
        return _search_local(domain, query, top_k)  # fallback


def format_retrieved_context(docs: list[RetrievedDoc]) -> str:
    """Format retrieved documents into a context string for the LLM."""
    if not docs:
        return "(No relevant knowledge found)"

    sections = []
    for doc in docs:
        header = f"[{doc.domain.upper()}] {doc.title}" if doc.title else f"[{doc.domain.upper()}]"
        sections.append(f"### {header}\n{doc.content}")
    return "\n\n".join(sections)
