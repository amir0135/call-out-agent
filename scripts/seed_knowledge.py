#!/usr/bin/env python3
"""Seed Azure AI Search indexes with sample knowledge base documents.

Usage:
    # Set AZURE_AI_SEARCH_ENDPOINT in .env or environment
    python scripts/seed_knowledge.py

    # Or pass endpoint directly
    python scripts/seed_knowledge.py --endpoint https://search-xxx.search.windows.net

This script creates the indexes (if not present) and uploads sample
documents for local development and testing. In production, these
indexes would be populated from Foundry IQ data pipelines.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

# Sample documents per domain — mirrors the local KB in knowledge_retriever.py
# but formatted for Azure AI Search upload.
SAMPLE_DOCS: dict[str, list[dict]] = {
    "idx-equipment-manuals": [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "equip-1")),
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
            "domain": "equipment",
            "source": "RC-550 Manual v3.2",
            "metadata": {"equipment_model": "RC-550", "alarm_type": "high_temperature"},
        },
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "equip-2")),
            "title": "Contoso RCU-100 Condensing Unit — Troubleshooting",
            "content": (
                "If discharge pressure is high: clean condenser coil, check fan motor. "
                "If suction pressure is low: check for refrigerant leak, inspect expansion valve. "
                "Unit not starting: verify power supply, check contactor, inspect overload relay. "
                "Compressor short-cycling: check high-pressure cutout, verify charge level. "
                "Noise/vibration: check mounting bolts, inspect compressor internals."
            ),
            "domain": "equipment",
            "source": "RCU-100 Troubleshooting Guide",
            "metadata": {"equipment_model": "RCU-100", "alarm_type": ""},
        },
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "equip-3")),
            "title": "Contoso RT-202 — Temperature Controller Specifications",
            "content": (
                "RT-202 single-stage controller for refrigeration. "
                "Temperature range: -60°C to +40°C. Resolution: 0.1°C. "
                "Digital input for door switch monitoring. "
                "Alarm outputs: high temp, low temp, door open. "
                "Defrost types: off-cycle, electric, hot gas. "
                "Maximum alarm delay configurable: 0-240 minutes. "
                "Sensor type: NTC/PTC, 2-wire connection."
            ),
            "domain": "equipment",
            "source": "RT-202 Datasheet",
            "metadata": {"equipment_model": "RT-202", "alarm_type": ""},
        },
    ],
    "idx-maintenance-guides": [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "maint-1")),
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
            "domain": "maintenance",
            "source": "Maintenance SOP Library",
            "metadata": {"alarm_type": "high_temperature"},
        },
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "maint-2")),
            "title": "Preventive Maintenance Schedule — Refrigeration",
            "content": (
                "Weekly: Check display temperatures, verify no active alarms. "
                "Monthly: Clean condenser coils, check door gaskets, verify defrost drain. "
                "Quarterly: Check refrigerant charge, inspect electrical connections, "
                "calibrate temperature sensors. "
                "Annually: Full system inspection, compressor oil analysis, "
                "replace air filters, test safety devices."
            ),
            "domain": "maintenance",
            "source": "PM Schedule v2.1",
            "metadata": {"alarm_type": ""},
        },
    ],
    "idx-store-profiles": [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "store-1")),
            "title": "Store Profile — Copenhagen Central Market",
            "content": (
                "Store ID: STORE-CPH-001. Location: Copenhagen, Denmark. "
                "Equipment: 4x medium-temp display cases (RC-550), "
                "2x low-temp freezer cases (RT-202), 1x RCU-100 condensing unit. "
                "Service contract: Premium — direct technician dispatch after 1 failed call. "
                "Primary contact: Store Manager. "
                "Operating hours: 07:00-22:00 CET. "
                "Alarm history: 3 high-temp alarms in last 90 days (2 door-related, 1 defrost)."
            ),
            "domain": "store_profiles",
            "source": "Store Database",
            "metadata": {"store_id": "STORE-CPH-001"},
        },
    ],
    "idx-alarm-history": [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "alarm-1")),
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
            "domain": "alarm_history",
            "source": "Analytics Report Q4 2024",
            "metadata": {"alarm_type": "high_temperature"},
        },
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "alarm-2")),
            "title": "Seasonal Alarm Trends",
            "content": (
                "High temperature alarms increase 35% during summer months (Jun-Aug). "
                "Peak alarm hours: 14:00-17:00 local time. "
                "Stores with monthly condenser cleaning show 40% fewer high-temp alarms. "
                "Stores with >5 years old equipment: 2.3x higher alarm rate. "
                "Average resolution time: door issues 15 min, defrost 45 min, "
                "compressor >4 hours."
            ),
            "domain": "alarm_history",
            "source": "Analytics Report Q4 2024",
            "metadata": {"alarm_type": "high_temperature"},
        },
    ],
}


def create_index(endpoint: str, index_name: str, headers: dict) -> None:
    """Create an Azure AI Search index if it doesn't exist."""
    import httpx

    schema = {
        "name": index_name,
        "fields": [
            {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
            {"name": "title", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "content", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "domain", "type": "Edm.String", "filterable": True, "facetable": True, "retrievable": True},
            {"name": "source", "type": "Edm.String", "filterable": True, "retrievable": True},
            {"name": "metadata", "type": "Edm.String", "retrievable": True},
        ],
        "semantic": {
            "configurations": [
                {
                    "name": f"{index_name}-semantic",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "title"},
                        "contentFields": [{"fieldName": "content"}],
                    },
                }
            ]
        },
    }

    url = f"{endpoint}/indexes/{index_name}?api-version=2024-07-01"

    with httpx.Client(timeout=30.0) as client:
        # Check if exists
        resp = client.get(url, headers=headers)
        if resp.status_code == 200:
            print(f"  Index '{index_name}' already exists — skipping creation")
            return

        # Create
        create_url = f"{endpoint}/indexes?api-version=2024-07-01"
        resp = client.post(create_url, json=schema, headers=headers)
        resp.raise_for_status()
        print(f"  Created index '{index_name}'")


def upload_documents(endpoint: str, index_name: str, docs: list[dict], headers: dict) -> None:
    """Upload documents to an Azure AI Search index."""
    import httpx

    # Convert metadata dict to JSON string for simple Edm.String field
    formatted = []
    for doc in docs:
        d = dict(doc)
        if isinstance(d.get("metadata"), dict):
            d["metadata"] = json.dumps(d["metadata"])
        d["@search.action"] = "mergeOrUpload"
        formatted.append(d)

    url = f"{endpoint}/indexes/{index_name}/docs/index?api-version=2024-07-01"

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json={"value": formatted}, headers=headers)
        resp.raise_for_status()
        results = resp.json()
        succeeded = sum(1 for r in results.get("value", []) if r.get("status"))
        print(f"  Uploaded {succeeded}/{len(formatted)} documents to '{index_name}'")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Azure AI Search knowledge indexes")
    parser.add_argument("--endpoint", default=os.environ.get("AZURE_AI_SEARCH_ENDPOINT", ""))
    args = parser.parse_args()

    endpoint = args.endpoint.rstrip("/")
    if not endpoint:
        print("Error: Set AZURE_AI_SEARCH_ENDPOINT or pass --endpoint")
        sys.exit(1)

    # Authenticate with DefaultAzureCredential
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    token = credential.get_token("https://search.azure.com/.default")
    headers = {
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json",
    }

    print(f"Seeding knowledge indexes at {endpoint}\n")

    for index_name, docs in SAMPLE_DOCS.items():
        print(f"[{index_name}]")
        create_index(endpoint, index_name, headers)
        upload_documents(endpoint, index_name, docs, headers)
        print()

    print("Done! Knowledge indexes are ready for agentic RAG.")


if __name__ == "__main__":
    main()
