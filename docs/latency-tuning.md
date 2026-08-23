# Latency Tuning Guide — Call-Out Agent on Azure Communication Services

**Audience:** Syed's team
**Author:** Amira (with Henrik)
**Date:** 22 May 2026
**Status:** Validated on live call `7cae7a62-b61e-41e0-9b5f-52364913b67e` (22 May 2026, 12:39 UTC)

> **⚠️ Update (July 2026):** the recognize/play loop analyzed below has been **superseded** by ACS bidirectional media streaming (`STREAMING_MODE=true`) — [§4 option 2](#4-getting-from-2s-to-sub-second) is what got built. Three engines are switchable via `STREAMING_ENGINE`, all live-verified:
>
> | Engine | Pipeline | Measured voice-to-voice / first-audio |
> |---|---|---|
> | `chained` (pilot default) | Speech STT → SOP engine + agent → Speech TTS | **~340–480 ms** v2v, 100% under the 800 ms target |
> | `realtime` | gpt-realtime-mini speech-to-speech | ~560–800 ms first-audio |
> | `voicelive` | Voice Live API + Azure neural voice | ~650–950 ms first-audio |
>
> The rest of this document is kept as the reference for the **legacy recognize-based flow** and its tuning knobs (still used when `STREAMING_MODE=false`).

---

## TL;DR

| Metric | Before tuning | After tuning (today) | Target |
|---|---|---|---|
| Per-turn latency (caller finishes speaking → next prompt audio) | 3–5 s | **2.0–3.9 s** | 700–900 ms |
| Orchestrator decision time (SOP match + render) | n/a | **7–16 ms** | <50 ms |
| End-silence detection after caller stops | 1.5 s (default) | **1 s** (yes/no) / **2 s** (open) | 500–800 ms |
| ACS TTS render → first audio byte | 2–4 s | **1.5–3.4 s** | 400–800 ms (Custom Voice or pre-roll) |

The orchestrator/agent side is **effectively free** (<20 ms). All remaining user-perceived lag is on the ACS side: end-of-speech detection and TTS render.

To hit sub-second the only remaining lever inside our control is **end-silence timeout** (already pushed to the practical minimum for STT). To go further requires either (a) pre-rendered audio for fixed prompts, (b) ACS bidirectional streaming + custom STT/TTS, or (c) switching to a provider with a streaming TTS API. See [§4](#4-getting-from-2s-to-sub-second).

---

## 1. Where the latency lives

Measured on a live call against `+45 29 22 94 22` from East US ACS, Mac in Copenhagen on residential broadband:

```
caller stops speaking
   │
   │  end_silence_timeout            ← configurable (we set to 1 s for yes/no)
   ▼
ACS emits RecognizeCompleted
   │
   │  webhook → orchestrator → SOP match → next utterance
   │  ≈ 7–16 ms total                 ← measured, not estimated
   ▼
orchestrator calls /:recognize and /:play
   │
   │  TTS synthesis + first audio chunk
   │  ≈ 1.5–3.4 s                     ← ACS-side, not directly controllable
   ▼
caller hears the next prompt
```

**Total per-turn latency we measured today:** 3.9 s on the first turn, **2.0–2.1 s on subsequent turns** (TTS warm). End-to-end an 8-step SOP completed in 1 min 25 s with three caller responses.

---

## 2. Configuration variables (with current values)

All knobs live in environment variables, set in `.env` for local runs and in `infra/main.bicep` for Container Apps deployments.

### 2.1 ACS recognize timeouts

| Variable | Default | What it controls | Recommendation |
|---|---|---|---|
| `ACS_END_SILENCE_TIMEOUT` | `1.5` (→ rounds to **2 s**) | Silence after caller stops before ACS emits `RecognizeCompleted`. Used for open-ended questions. | `1` for English yes/no/short answers; `2` for free-form responses (descriptions, addresses). |
| `ACS_END_SILENCE_TIMEOUT_FAST` | `0.8` (→ rounds to **1 s**) | Same, but used only for yes/no SOP steps via `end_silence_timeout_override`. | Keep at `1`. ACS won't accept lower than 1 second (the SDK casts internally; see [Known issue](#5-known-issues--gotchas)). |
| `ACS_INITIAL_SILENCE_TIMEOUT` | `10` | How long ACS will wait *before* the caller starts speaking before declaring `InitialSilenceTimeout`. | `8–10` for human callers. <5 produces false negatives when the caller is still processing the question. |
| `ACS_INTERRUPT_PROMPT` (barge-in) | `false` | If `true`, caller can interrupt the prompt; recognition starts during play. | Leave `false` until you've validated line quality. Background phone noise commonly triggers partial recognize → `RecognizeFailed` and kills the turn. |
| `ACS_ENABLE_DTMF_FALLBACK` | `true` | Recognize accepts speech **or** a single keypress (1/2/9). | Keep on. Safety net for accents, noisy lines, hearing-impaired callers. |

### 2.2 TTS

| Variable | Default | What it controls | Recommendation |
|---|---|---|---|
| `SPEECH_RATE` | `+5%` | SSML `prosody rate` applied to every utterance. Shorter audio → shorter play time. | `+5%` to `+10%` is the sweet spot. Above `+15%` users complain it sounds rushed. |
| `SPEECH_VOICE` (env, not yet set) | `en-US-JennyNeural` | Neural voice. | Test `en-US-AndrewMultilingualNeural` — typically lower first-byte latency than Jenny. |

### 2.3 Agent / LLM

| Variable | Default | What it controls | Recommendation |
|---|---|---|---|
| `LOCAL_MODE` | `true` for dev | When `true`, the orchestrator uses the **rule-based** agent (no LLM call). | Use for fixed SOP flows. SOP matching is sub-millisecond. Only set `false` when you need free-form LLM reasoning. |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o-mini` | LLM behind the Foundry agent when `LOCAL_MODE=false`. | `gpt-4o-mini` adds ~150–400 ms per turn vs ~0 ms for rule-based. Avoid `gpt-4o` unless you actually need its reasoning — adds 600 ms–1.5 s. |

---

## 3. What we already did (and the gain we measured)

| Change | Where | Measured impact |
|---|---|---|
| `gpt-4o` → `gpt-4o-mini` | `infra/modules/openai.bicep` | -300 to -1000 ms per turn (when LLM in loop) |
| `LOCAL_MODE=true` rule-based agent for fixed SOPs | `src/agent/agent.py`, `src/orchestrator/app.py` | Reduces agent step to **7–16 ms** (we don't call the LLM at all) |
| SSML `<prosody rate="+5%">` on all utterances | `src/orchestrator/call_handler.py` `_build_text_source` | Shorter playback ≈ 5 % less wall-clock per turn |
| `expected_response` per SOP step (`yes_no` / `open`) | `sops/high_temp_alarm.json`, `src/orchestrator/sop_engine.py` | Enables adaptive end-silence below |
| Adaptive end-silence (`1 s` for yes/no, `2 s` for open) | `src/orchestrator/app.py` `_handle_play_completed` | -500 to -700 ms on yes/no turns |
| `ACS_INTERRUPT_PROMPT=false` (barge-in off) | `.env`, Bicep | Eliminated the `RecognizeFailed` storms we were seeing from noise-triggered partial recognitions |

---

## 4. Getting from 2 s to sub-second

The remaining 2 s/turn breaks down roughly:
- ~1 s end-silence detection (ACS minimum we observed)
- ~1.5–3.4 s ACS TTS render to first audio byte
- ~0.5 s recognize re-arm gap

### Options ranked by effort

1. **Pre-render fixed prompts to audio files (low effort, high gain).**
   Greeting and closing utterances are identical on every call. Render them once with `cognitiveservices` Speech SDK, store in Blob, and pass a `FileSource(url=...)` to `play_media` instead of `TextSource`. First-byte latency for file playback is typically 200–400 ms vs 1.5–3.4 s for live synthesis.

2. **Use ACS Bidirectional Streaming + your own STT/TTS (medium effort, highest gain).**
   ACS supports `media_streaming` to a WebSocket. Pipe to Azure Speech with continuous recognition (no end-silence detection needed — you decide when the user is done from word-level events) and Azure Neural TTS streaming (first audio byte ~300 ms). End-to-end ~600–900 ms is achievable. Requires a stateful WebSocket worker, not a stateless HTTP webhook. Documented at <https://learn.microsoft.com/azure/communication-services/concepts/call-automation/audio-streaming-concept>.

3. **Speech custom endpoint in same region as ACS deployment (low effort, ~100–300 ms).**
   Today `SPEECH_REGION=eastus` and ACS is in East US — already optimal for US callers. For DK/EU callers, redeploy to `westeurope` or `northeurope`. Cross-Atlantic alone is ~100–150 ms RTT.

4. **Twilio comparison.** Twilio Voice + Twilio Speech is a valid baseline. Their `<Gather>` verb has similar end-silence semantics (`speechTimeout="auto"` ≈ 1 s) and their `<Say>` neural voices have comparable first-byte latency. The architectural pattern below works identically against either provider — the only code that changes is `src/orchestrator/call_handler.py`. **We do not have benchmark data comparing the two; happy to set up a side-by-side if useful.**

---

## 5. Known issues / gotchas

- **ACS recognize parameters must be integers.** The SDK type hint is `Optional[int]` and the server rejects floats with HTTP 400. We cast via `max(1, round(x))` in `start_recognize_speech`. Practically this means `0.8` becomes `1` — you cannot configure sub-second end-silence on ACS today.
- **`dtmf_stop_tones` requires the `DtmfTone` enum, not the literal `"#"` string.** ACS returns 400 `Error converting value '#' to type ...Tone` otherwise.
- **SSML must be passed via `SsmlSource(ssml_text=...)`, not `TextSource(text=...)`.** `TextSource` validates as plain text and rejects SSML with 400 `(8523) TextPrompt either SourceLocale or VoiceName needs to be provided`.
- **Barge-in (`interrupt_prompt=true`) is fragile on landlines and cellular.** Background noise triggers partial recognitions that immediately fail and burn one of the unclear-response retries. Keep `false` until you have a streaming pipeline that can distinguish noise from speech.
- **Connection-string env vars containing `;`** must be quoted in `.env` or `bash` `source` will split on them.

---

## 6. Quick wins for Syed's team to test

In priority order:

1. Set `ACS_END_SILENCE_TIMEOUT=1` (was 1.5/2) in your environment. Saves ~500 ms on every open-ended turn.
2. Set `LOCAL_MODE=true` if your SOP is fully scripted. Saves 300–1000 ms per turn vs LLM in loop.
3. Pre-render the greeting and closing prompts to Blob and play via `FileSource`. Saves ~2 s on first turn.
4. Confirm ACS region matches your callers' region. East US for North America, West Europe / North Europe for EU.
5. Only then revisit barge-in (`ACS_INTERRUPT_PROMPT=true`) — and only with line-quality monitoring in place.

Happy to pair on any of these.
