#!/usr/bin/env python3
"""Pre-render fixed SOP prompts to audio files to cut TTS latency.

Prompts with no ``{placeholders}`` are identical on every call, so we
synthesize them once to WAV and play them via ACS ``FileSource`` (~0.3s
first byte) instead of live TTS (~2.5s). Dynamic prompts (greeting,
awareness — they contain {store_name}/{equipment_name}) still use live
TTS and are skipped here.

Auth: the Speech resource is AAD-only (disableLocalAuth=true). We use
DefaultAzureCredential + the resource-scoped Bearer format.

Usage:
    python scripts/prerender_prompts.py
    # writes prerendered/<hash>.wav + prerendered/manifest.json

Re-run after editing any fixed SOP prompt text.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential

_REPO = Path(__file__).resolve().parent.parent
_SOP_DIR = _REPO / "sops"
_OUT_DIR = _REPO / "prerendered"

REGION = os.environ.get("SPEECH_REGION", "eastus")
VOICE = os.environ.get("SPEECH_VOICE", "en-US-JennyNeural")
ENDPOINT = os.environ.get("COGNITIVE_SERVICES_ENDPOINT", "")
# riff-24khz mono PCM — what ACS plays back cleanly.
OUTPUT_FORMAT = "riff-24khz-16bit-mono-pcm"


def _contoso(text: str) -> str:
    """Match the orchestrator's brand-name pronunciation tweak."""
    import re

    return re.sub(r"(?i)\bcontoso\b", "Con-toso", text)


def _resource_id() -> str:
    """Resolve the Speech resource ARM id (env override, else az lookup)."""
    rid = os.environ.get("SPEECH_RESOURCE_ID", "")
    if rid:
        return rid
    host = ENDPOINT.split("//")[-1].split(".")[0]
    out = subprocess.check_output(
        [
            "az", "cognitiveservices", "account", "list",
            "--query", f"[?contains(properties.endpoint, '{host}')].id",
            "-o", "tsv",
        ],
        text=True,
    ).strip()
    if not out:
        sys.exit("Could not resolve Speech resource id; set SPEECH_RESOURCE_ID.")
    return out.splitlines()[0]


def _fixed_prompts() -> list[str]:
    """Collect unique SOP prompt texts that contain no {placeholders}."""
    texts: list[str] = []
    seen: set[str] = set()
    for sop_path in sorted(_SOP_DIR.glob("*.json")):
        try:
            doc = json.loads(sop_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if "steps" not in doc:
            continue
        for step in doc.get("steps", []):
            text = (step.get("text") or "").strip()
            if not text or "{" in text:  # skip dynamic prompts
                continue
            if text not in seen:
                seen.add(text)
                texts.append(text)
    return texts


def _synthesize(text: str, bearer: str) -> bytes:
    ssml = (
        "<speak version='1.0' xml:lang='en-US'>"
        f"<voice name='{VOICE}'>{_contoso(text)}</voice>"
        "</speak>"
    )
    url = f"https://{REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": OUTPUT_FORMAT,
        },
        content=ssml.encode("utf-8"),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def main() -> None:
    prompts = _fixed_prompts()
    if not prompts:
        sys.exit("No fixed prompts found to pre-render.")

    rid = _resource_id()
    token = DefaultAzureCredential().get_token(
        "https://cognitiveservices.azure.com/.default"
    ).token
    bearer = f"aad#{rid}#{token}"

    _OUT_DIR.mkdir(exist_ok=True)
    manifest: dict[str, str] = {}
    for text in prompts:
        digest = hashlib.sha1(f"{VOICE}|{text}".encode("utf-8")).hexdigest()[:16]
        fname = f"{digest}.wav"
        audio = _synthesize(text, bearer)
        (_OUT_DIR / fname).write_bytes(audio)
        manifest[text] = fname
        print(f"  ✓ {fname}  ({len(audio):>7} bytes)  {text[:55]}…")

    (_OUT_DIR / "manifest.json").write_text(
        json.dumps({"voice": VOICE, "prompts": manifest}, indent=2),
        encoding="utf-8",
    )
    print(f"\nPre-rendered {len(prompts)} fixed prompt(s) → {_OUT_DIR}")
    print("manifest.json written. Restart the orchestrator to pick them up.")


if __name__ == "__main__":
    main()
