"""Region routing for global, low-latency outbound calls.

Callers/callees are spread across the world. PSTN media latency is
dominated by the ACS resource's region (where the call hands off to the
phone network), and TTS/STT latency by the Speech region. So each call
should be placed from the ACS resource + caller-ID number + Speech region
**nearest the callee**, chosen from the callee's E.164 country code.

A single-region deployment needs no config: ``create_region_router`` falls
back to one ``"default"`` region built from the standard env vars, so
existing behaviour is unchanged. Provide ``sops/regions.json`` (or set
``REGIONS_CONFIG_PATH``) to activate multi-region routing.

Example ``sops/regions.json``::

    {
      "default_region": "na",
      "regions": [
        {
          "region_id": "na",
          "acs_endpoint": "https://contoso-acs.unitedstates.communication.azure.com",
          "source_phone": "+18005550100",
          "dial_prefixes": ["+1"],
          "speech_region": "eastus",
          "speech_voice": "en-US-JennyNeural",
          "cognitive_services_endpoint": "https://cog-eastus.cognitiveservices.azure.com"
        },
        {
          "region_id": "eu",
          "acs_endpoint": "https://contoso-acs-eu.europe.communication.azure.com",
          "source_phone": "+4589870000",
          "dial_prefixes": ["+45", "+44", "+49", "+33", "+34", "+31"],
          "speech_region": "westeurope",
          "speech_voice": "en-GB-LibbyNeural",
          "cognitive_services_endpoint": "https://cog-westeurope.cognitiveservices.azure.com"
        }
      ]
    }
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "sops" / "regions.json"


class RegionConfig(BaseModel):
    """Per-region telephony + speech configuration for one geography."""

    region_id: str
    acs_endpoint: str
    source_phone: str
    # E.164 dial-code prefixes this region serves (longest match wins),
    # e.g. ["+45", "+44", "+49"]. Empty => only used as the default region.
    dial_prefixes: list[str] = Field(default_factory=list)
    speech_region: str = ""
    speech_voice: str = ""
    cognitive_services_endpoint: str = ""
    # Optional connection-string auth for this region's ACS resource.
    # When empty, the handler uses DefaultAzureCredential (AAD).
    acs_connection_string: str = ""


class RegionRouter:
    """Resolves the nearest region for a callee phone number."""

    def __init__(self, regions: list[RegionConfig], default_region_id: str):
        if not regions:
            raise ValueError("RegionRouter requires at least one region")
        self._by_id: dict[str, RegionConfig] = {r.region_id: r for r in regions}
        if default_region_id not in self._by_id:
            raise ValueError(
                f"default region '{default_region_id}' is not among configured regions"
            )
        self._default_id = default_region_id
        # (prefix, region_id) sorted longest-first so the most specific
        # dial code wins (e.g. "+1" vs a hypothetical "+1340").
        self._prefixes: list[tuple[str, str]] = sorted(
            ((p, r.region_id) for r in regions for p in r.dial_prefixes),
            key=lambda t: len(t[0]),
            reverse=True,
        )

    @property
    def default_region_id(self) -> str:
        return self._default_id

    @property
    def region_ids(self) -> list[str]:
        return list(self._by_id)

    @property
    def is_multi_region(self) -> bool:
        return len(self._by_id) > 1

    def get(self, region_id: str | None) -> RegionConfig:
        """Return a region by id, falling back to the default region."""
        if region_id and region_id in self._by_id:
            return self._by_id[region_id]
        return self._by_id[self._default_id]

    def default(self) -> RegionConfig:
        return self._by_id[self._default_id]

    def resolve(self, phone_number: str | None) -> RegionConfig:
        """Pick the region nearest a callee E.164 number by dial code."""
        if phone_number:
            normalized = phone_number.strip().replace(" ", "").replace("-", "")
            for prefix, region_id in self._prefixes:
                if normalized.startswith(prefix):
                    return self._by_id[region_id]
        return self._by_id[self._default_id]


def create_region_router(
    *,
    default_acs_endpoint: str,
    default_source_phone: str,
    default_speech_region: str = "",
    default_speech_voice: str = "",
    default_cognitive_services_endpoint: str = "",
    default_acs_connection_string: str = "",
    config_path: str | os.PathLike[str] | None = None,
) -> RegionRouter:
    """Build a router from JSON config when present, else a single region.

    Backward compatible: with no ``regions.json`` the router contains one
    ``"default"`` region constructed from the supplied env-derived values,
    so single-region deployments behave exactly as before.
    """
    path_str = str(config_path) if config_path else os.environ.get("REGIONS_CONFIG_PATH", "")
    candidate = Path(path_str) if path_str else _DEFAULT_CONFIG_PATH

    if candidate.exists():
        try:
            data = json.loads(candidate.read_text())
            regions = [RegionConfig(**r) for r in data.get("regions", [])]
            if regions:
                default_id = data.get("default_region", regions[0].region_id)
                logger.info(
                    "Region router: loaded %d region(s) from %s (default=%s, multi-region)",
                    len(regions),
                    candidate,
                    default_id,
                )
                return RegionRouter(regions, default_id)
            logger.warning("Regions config %s has no regions — using single default", candidate)
        except Exception as exc:  # noqa: BLE001 - config is operator-provided
            logger.warning(
                "Failed to load regions config %s: %s — falling back to single region",
                candidate,
                exc,
            )

    default = RegionConfig(
        region_id="default",
        acs_endpoint=default_acs_endpoint,
        source_phone=default_source_phone,
        speech_region=default_speech_region,
        speech_voice=default_speech_voice,
        cognitive_services_endpoint=default_cognitive_services_endpoint,
        acs_connection_string=default_acs_connection_string,
    )
    logger.info("Region router: single default region (no multi-region config found)")
    return RegionRouter([default], "default")
