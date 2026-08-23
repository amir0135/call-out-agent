"""Tests for the voice-to-voice latency tracker."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.latency import TARGET_MS, LatencyTracker


class TestLatencyTracker:
    def test_voice_to_voice_includes_end_silence(self):
        t = LatencyTracker()
        # Caller's true speech end is 1.0s before RecognizeCompleted (the
        # end_silence). Recognize completes at t=10.0, response audio at t=10.4.
        t.recognize_completed("c1", "ask_awareness", end_silence_s=1.0, when=10.0)
        sample = t.response_audio_started("c1", when=10.4)
        assert sample is not None
        # v2v = (10.4 - (10.0 - 1.0)) * 1000 = 1400ms
        assert sample["v2v_ms"] == 1400.0
        # reaction (recognize->audio) = 0.4s = 400ms
        assert sample["reaction_ms"] == 400.0
        assert sample["within_target"] is False

    def test_within_target(self):
        t = LatencyTracker()
        t.recognize_completed("c1", "confirm", end_silence_s=0.25, when=5.0)
        sample = t.response_audio_started("c1", when=5.4)
        # v2v = (5.4 - 4.75)*1000 = 650ms < 800
        assert sample["v2v_ms"] == 650.0
        assert sample["within_target"] is True

    def test_play_without_recognize_is_ignored(self):
        # e.g. the greeting PlayStarted has no preceding RecognizeCompleted.
        t = LatencyTracker()
        assert t.response_audio_started("c1", when=1.0) is None
        assert t.stats()["count"] == 0

    def test_stats_percentiles(self):
        t = LatencyTracker()
        # Produce samples with v2v of 200,400,600,800,2000 ms.
        for i, gap in enumerate([0.2, 0.4, 0.6, 0.8, 2.0]):
            cid = f"c{i}"
            t.recognize_completed(cid, "step", end_silence_s=0.0, when=0.0)
            t.response_audio_started(cid, when=gap)
        s = t.stats()
        assert s["count"] == 5
        assert s["min_ms"] == 200.0
        assert s["max_ms"] == 2000.0
        assert s["target_ms"] == TARGET_MS
        # 4 of 5 within 800ms target = 80%
        assert s["within_target_pct"] == 80.0
        assert "p95_ms" in s and "p50_ms" in s

    def test_empty_stats(self):
        assert LatencyTracker().stats() == {"count": 0, "target_ms": TARGET_MS}
