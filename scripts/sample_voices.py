#!/usr/bin/env python3
"""Render a sample line in several candidate voices for quick A/B listening.

Synthesizes the same greeting-style sentence in each candidate voice to
``voice_samples/<voice>.wav`` so you can compare them on your machine
(``afplay``) without placing a phone call for each one. Pick a winner, then
set ``SPEECH_VOICE=<voice>`` in .env and restart.

Auth matches prerender_prompts.py: AAD Bearer (resource is local-auth
disabled). Override the sentence with SAMPLE_TEXT, or the voice list with
SAMPLE_VOICES (comma-separated).

Usage:
    python scripts/sample_voices.py
    # then: afplay voice_samples/en-US-AvaMultilingualNeural.wav
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential

_REPO = Path(__file__).resolve().parent.parent
_OUT_DIR = _REPO / "voice_samples"

REGION = os.environ.get("SPEECH_REGION", "eastus")
ENDPOINT = os.environ.get("COGNITIVE_SERVICES_ENDPOINT", "")
OUTPUT_FORMAT = "riff-24khz-16bit-mono-pcm"

# A representative greeting + first question, mirroring the live call's tone.
SAMPLE_TEXT = os.environ.get(
    "SAMPLE_TEXT",
    "Hi, this is the Contoso monitoring service calling about a high "
    "temperature alarm on one of your units. Sorry to bother you. "
    "Were you already aware of this alarm?",
)

# The most natural, conversational Azure neural voices. Multilingual voices
# are the newest generation and sound markedly more human than JennyNeural.
DEFAULT_VOICES = [
    "en-US-AvaMultilingualNeural",     # warm, natural female — top pick
    "en-US-EmmaMultilingualNeural",    # friendly, upbeat female
    "en-US-AndrewMultilingualNeural",  # warm, relaxed male
    "en-US-BrianMultilingualNeural",   # casual, conversational male
    "en-GB-AdaMultilingualNeural",     # natural UK female
    "en-GB-OllieMultilingualNeural",   # natural UK male
    "en-US-JennyNeural",               # current voice, for comparison
]

VOICES = [
    v.strip()
    for v in os.environ.get("SAMPLE_VOICES", ",".join(DEFAULT_VOICES)).split(",")
    if v.strip()
]


def _contoso(text: str) -> str:
    return re.sub(r"(?i)\bcontoso\b", "Con-toso", text)


def _resource_id() -> str:
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


def _synthesize(text: str, voice: str, bearer: str) -> bytes:
    lang = "-".join(voice.split("-")[:2])  # e.g. en-US / en-GB
    ssml = (
        f"<speak version='1.0' xml:lang='{lang}'>"
        f"<voice name='{voice}'>{_contoso(text)}</voice>"
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
    rid = _resource_id()
    token = DefaultAzureCredential().get_token(
        "https://cognitiveservices.azure.com/.default"
    ).token
    bearer = f"aad#{rid}#{token}"

    _OUT_DIR.mkdir(exist_ok=True)
    print(f'Sample line: "{SAMPLE_TEXT}"\n')
    written: list[Path] = []
    for voice in VOICES:
        try:
            audio = _synthesize(SAMPLE_TEXT, voice, bearer)
        except httpx.HTTPStatusError as exc:
            print(f"  ✗ {voice}: {exc.response.status_code} (voice unavailable in {REGION}?)")
            continue
        path = _OUT_DIR / f"{voice}.wav"
        path.write_bytes(audio)
        written.append(path)
        print(f"  ✓ {voice}  ({len(audio):>7} bytes)")

    if not written:
        sys.exit("No samples rendered.")

    print(f"\n{len(written)} sample(s) → {_OUT_DIR}")
    print("Listen (macOS):")
    for path in written:
        print(f"  afplay '{path.relative_to(_REPO)}'")
    print("\nPick one, then set in .env:  SPEECH_VOICE=<voice>  and restart.")


if __name__ == "__main__":
    main()
