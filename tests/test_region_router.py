"""Tests for region routing (multi-region outbound call placement)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.region_router import (
    RegionConfig,
    RegionRouter,
    create_region_router,
)


def _na() -> RegionConfig:
    return RegionConfig(
        region_id="na",
        acs_endpoint="https://acs-na.example.com",
        source_phone="+18005550100",
        dial_prefixes=["+1"],
        speech_region="eastus",
        speech_voice="en-US-JennyNeural",
    )


def _eu() -> RegionConfig:
    return RegionConfig(
        region_id="eu",
        acs_endpoint="https://acs-eu.example.com",
        source_phone="+4589870000",
        dial_prefixes=["+45", "+44", "+49"],
        speech_region="westeurope",
        speech_voice="en-GB-LibbyNeural",
    )


class TestRegionRouter:
    def test_resolve_by_dial_prefix(self):
        router = RegionRouter([_na(), _eu()], default_region_id="na")
        assert router.resolve("+4512345678").region_id == "eu"
        assert router.resolve("+447911123456").region_id == "eu"
        assert router.resolve("+12025550123").region_id == "na"

    def test_resolve_handles_spaces_and_dashes(self):
        router = RegionRouter([_na(), _eu()], default_region_id="na")
        assert router.resolve("+45 29 22 94 22").region_id == "eu"
        assert router.resolve("+1-202-555-0123").region_id == "na"

    def test_resolve_unknown_prefix_falls_back_to_default(self):
        router = RegionRouter([_na(), _eu()], default_region_id="na")
        # +81 (Japan) is not served by any region → default.
        assert router.resolve("+81312345678").region_id == "na"

    def test_resolve_none_returns_default(self):
        router = RegionRouter([_na(), _eu()], default_region_id="eu")
        assert router.resolve(None).region_id == "eu"

    def test_longest_prefix_wins(self):
        specific = RegionConfig(
            region_id="carib",
            acs_endpoint="https://acs-carib.example.com",
            source_phone="+18095550100",
            dial_prefixes=["+1809"],
        )
        router = RegionRouter([_na(), specific], default_region_id="na")
        # +1809 (Dominican Republic) must beat the generic +1.
        assert router.resolve("+18095551234").region_id == "carib"
        assert router.resolve("+12025550123").region_id == "na"

    def test_get_unknown_region_returns_default(self):
        router = RegionRouter([_na(), _eu()], default_region_id="na")
        assert router.get("does-not-exist").region_id == "na"
        assert router.get(None).region_id == "na"
        assert router.get("eu").region_id == "eu"

    def test_is_multi_region(self):
        assert RegionRouter([_na(), _eu()], "na").is_multi_region is True
        assert RegionRouter([_na()], "na").is_multi_region is False

    def test_invalid_default_raises(self):
        with pytest.raises(ValueError, match="default region"):
            RegionRouter([_na()], default_region_id="nope")

    def test_empty_regions_raises(self):
        with pytest.raises(ValueError, match="at least one region"):
            RegionRouter([], default_region_id="na")


class TestCreateRegionRouter:
    def test_single_region_fallback_when_no_config(self, tmp_path):
        missing = tmp_path / "nope.json"
        router = create_region_router(
            default_acs_endpoint="https://acs.example.com",
            default_source_phone="+10000000000",
            default_speech_region="eastus",
            config_path=missing,
        )
        assert router.is_multi_region is False
        assert router.default().region_id == "default"
        # Any number resolves to the single default region.
        assert router.resolve("+4512345678").region_id == "default"
        assert router.default().source_phone == "+10000000000"

    def test_loads_multi_region_from_config(self, tmp_path):
        cfg = {
            "default_region": "na",
            "regions": [
                {
                    "region_id": "na",
                    "acs_endpoint": "https://acs-na.example.com",
                    "source_phone": "+18005550100",
                    "dial_prefixes": ["+1"],
                },
                {
                    "region_id": "eu",
                    "acs_endpoint": "https://acs-eu.example.com",
                    "source_phone": "+4589870000",
                    "dial_prefixes": ["+45", "+44"],
                },
            ],
        }
        path = tmp_path / "regions.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")

        router = create_region_router(
            default_acs_endpoint="https://unused.example.com",
            default_source_phone="+19999999999",
            config_path=path,
        )
        assert router.is_multi_region is True
        assert sorted(router.region_ids) == ["eu", "na"]
        assert router.resolve("+4512345678").region_id == "eu"
        assert router.resolve("+12025550123").region_id == "na"
        assert router.default_region_id == "na"

    def test_malformed_config_falls_back_to_single_region(self, tmp_path):
        path = tmp_path / "regions.json"
        path.write_text("{ not valid json", encoding="utf-8")

        router = create_region_router(
            default_acs_endpoint="https://acs.example.com",
            default_source_phone="+10000000000",
            config_path=path,
        )
        assert router.is_multi_region is False
        assert router.default().region_id == "default"
