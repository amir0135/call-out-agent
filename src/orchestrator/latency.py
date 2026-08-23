"""Voice-to-voice latency instrumentation.

Measures the metric that actually matters for a voice agent: the time from
the *true* end of the caller's speech to the first audio frame of the
agent's response (a.k.a. time-to-first-audio).

We only have ACS webhook events to work from, so we approximate:

    true_end_of_speech ≈ RecognizeCompleted_receipt − end_silence_timeout

because ACS waits ``end_silence_timeout`` of silence *after* the caller
stops before emitting ``RecognizeCompleted``. The agent's response audio
is marked by the next ``PlayStarted`` event. So:

    voice_to_voice = PlayStarted_receipt − true_end_of_speech
                   = (PlayStarted − RecognizeCompleted) + end_silence_timeout

This is webhook-receipt based (it includes ACS→orchestrator delivery
time), so treat it as an upper-bound estimate, not a microbenchmark. We
report P95 because callers remember the worst turns, not the average.

Target (from product guidance): < 800 ms; 500–700 ms feels smooth.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("orchestrator.latency")

TARGET_MS = 800.0


@dataclass
class _Pending:
    recognize_completed: float  # monotonic seconds
    speech_end: float           # recognize_completed - end_silence
    step: str


class LatencyTracker:
    """Tracks per-turn voice-to-voice latency and aggregate stats."""

    def __init__(self, max_samples: int = 500):
        self._pending: dict[str, _Pending] = {}
        self._samples: list[dict] = []
        self._max = max_samples

    def recognize_completed(
        self,
        call_id: str,
        step: str,
        end_silence_s: float,
        when: float | None = None,
    ) -> None:
        """Mark that STT finalized for a turn (caller finished speaking)."""
        ts = time.monotonic() if when is None else when
        self._pending[call_id] = _Pending(
            recognize_completed=ts,
            speech_end=ts - max(0.0, end_silence_s),
            step=step,
        )

    def response_audio_started(self, call_id: str, when: float | None = None) -> dict | None:
        """Mark the agent's response audio starting; record a sample."""
        pend = self._pending.pop(call_id, None)
        if pend is None:
            return None  # a PlayStarted not preceded by a recognize (e.g. greeting)
        ts = time.monotonic() if when is None else when
        v2v_ms = (ts - pend.speech_end) * 1000.0
        reaction_ms = (ts - pend.recognize_completed) * 1000.0
        sample = {
            "call_id": call_id,
            "step": pend.step,
            "v2v_ms": round(v2v_ms, 1),
            "reaction_ms": round(reaction_ms, 1),
            "within_target": v2v_ms <= TARGET_MS,
        }
        self._samples.append(sample)
        if len(self._samples) > self._max:
            self._samples = self._samples[-self._max:]
        logger.info(
            "voice-to-voice: step=%s v2v=%.0fms reaction=%.0fms target=%.0fms %s",
            pend.step, v2v_ms, reaction_ms, TARGET_MS,
            "OK" if v2v_ms <= TARGET_MS else "OVER",
        )
        return sample

    @staticmethod
    def _percentile(sorted_vals: list[float], pct: float) -> float:
        if not sorted_vals:
            return 0.0
        k = int(round((pct / 100.0) * (len(sorted_vals) - 1)))
        return sorted_vals[max(0, min(len(sorted_vals) - 1, k))]

    def stats(self) -> dict:
        vals = [s["v2v_ms"] for s in self._samples]
        if not vals:
            return {"count": 0, "target_ms": TARGET_MS}
        sv = sorted(vals)
        return {
            "count": len(vals),
            "target_ms": TARGET_MS,
            "avg_ms": round(sum(vals) / len(vals), 1),
            "p50_ms": self._percentile(sv, 50),
            "p95_ms": self._percentile(sv, 95),
            "min_ms": min(vals),
            "max_ms": max(vals),
            "within_target_pct": round(100.0 * sum(1 for v in vals if v <= TARGET_MS) / len(vals), 1),
            "recent": self._samples[-10:],
        }
