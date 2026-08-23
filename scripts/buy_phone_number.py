#!/usr/bin/env python3
"""Search for and purchase an Azure Communication Services phone number.

Two modes:
  * search   (default) — free, commits nothing. Prints available numbers,
               their capabilities, and the MONTHLY cost + a search_id.
  * purchase — buys the numbers tied to a search_id (RECURRING CHARGE).

The number's country affects caller-ID presentation; call-media latency is
governed by the ACS resource's data location (contoso-acs-eu = Europe).

Auth: ACS connection string from --connection-string or env ACS_EU_CONNECTION_STRING.

Examples:
    # See what's available (no cost):
    python scripts/buy_phone_number.py search --country GB --type toll-free
    python scripts/buy_phone_number.py search --country GB --type geographic

    # Buy (recurring monthly cost) once you've reviewed a search:
    python scripts/buy_phone_number.py purchase --search-id <id-from-search>
"""

from __future__ import annotations

import argparse
import os
import sys

from azure.communication.phonenumbers import (
    PhoneNumbersClient,
    PhoneNumberAssignmentType,
    PhoneNumberCapabilities,
    PhoneNumberCapabilityType,
    PhoneNumberType,
)


def _client(conn: str) -> PhoneNumbersClient:
    return PhoneNumbersClient.from_connection_string(conn)


def _conn(args) -> str:
    conn = args.connection_string or os.environ.get("ACS_EU_CONNECTION_STRING", "")
    if not conn:
        sys.exit(
            "No connection string. Pass --connection-string or set "
            "ACS_EU_CONNECTION_STRING (az communication list-key "
            "--name contoso-acs-eu -g rg-contoso-callout --query "
            "primaryConnectionString -o tsv)."
        )
    return conn


def search(args) -> None:
    client = _client(_conn(args))
    num_type = (
        PhoneNumberType.TOLL_FREE
        if args.type == "toll-free"
        else PhoneNumberType.GEOGRAPHIC
    )
    assignment = (
        PhoneNumberAssignmentType.APPLICATION
        if args.assignment == "application"
        else PhoneNumberAssignmentType.PERSON
    )
    # Outbound calling for the agent; no SMS.
    caps = PhoneNumberCapabilities(
        calling=PhoneNumberCapabilityType.OUTBOUND,
        sms=PhoneNumberCapabilityType.NONE,
    )

    print(
        f"Searching: country={args.country} type={args.type} "
        f"assignment={args.assignment} calling=OUTBOUND ..."
    )
    poller = client.begin_search_available_phone_numbers(
        args.country,
        num_type,
        assignment,
        caps,
        area_code=args.area_code or None,
        quantity=args.quantity,
    )
    result = poller.result()

    cost = result.cost
    print("\n--- RESULT (nothing purchased yet) ---")
    print(f"  numbers   : {', '.join(result.phone_numbers) or '(none)'}")
    print(
        f"  monthly   : {cost.amount} {cost.currency_code} "
        f"per {getattr(cost, 'billing_frequency', 'month')}"
    )
    print(f"  search_id : {result.search_id}")
    print(f"  expires   : ~15 min (search_id only valid briefly)")
    print(
        "\nTo buy these EXACT numbers (recurring charge):\n"
        f"  python scripts/buy_phone_number.py purchase --search-id {result.search_id}"
    )


def purchase(args) -> None:
    client = _client(_conn(args))
    print(f"Purchasing search_id={args.search_id} (this incurs a recurring charge)...")
    poller = client.begin_purchase_phone_numbers(args.search_id)
    poller.result()
    print("✓ Purchase complete. List owned numbers:")
    for n in client.list_purchased_phone_numbers():
        print(f"  {n.phone_number}  calling={n.capabilities.calling} sms={n.capabilities.sms}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--connection-string", default="")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="Find available numbers (free).")
    s.add_argument("--country", required=True, help="ISO-2 country code, e.g. GB, IE, DK")
    s.add_argument("--type", choices=["toll-free", "geographic"], default="toll-free")
    s.add_argument("--assignment", choices=["application", "person"], default="application")
    s.add_argument("--area-code", default="")
    s.add_argument("--quantity", type=int, default=1)
    s.set_defaults(func=search)

    b = sub.add_parser("purchase", help="Buy numbers from a search_id (RECURRING COST).")
    b.add_argument("--search-id", required=True)
    b.set_defaults(func=purchase)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
