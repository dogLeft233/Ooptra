"""Bounded metadata-only voice decisions and honest stage measurements."""

from __future__ import annotations

import copy
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone


class VoiceObservation:
    def __init__(self, backend: str, limit: int = 40) -> None:
        self.backend = backend
        self._rows: deque[dict] = deque(maxlen=limit)

    def record(self, reason: str, outcome: str, *, started: float | None = None,
               timings: dict | None = None, timing_mode: str = "stages") -> None:
        self._rows.append({
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "backend": self.backend, "reason": reason, "outcome": outcome,
            "elapsed_ms": round(max(0, time.monotonic() - started) * 1000, 1) if started is not None else 0,
            "timings_ms": {stage: (timings or {}).get(stage) for stage in ("asr", "intent", "chat", "tts")},
            "timing_mode": timing_mode,
        })

    def snapshot(self) -> dict:
        return {"recent_decisions": copy.deepcopy(list(self._rows)), "limit": self._rows.maxlen}


class DecisionTrace:
    def __init__(self, observation: VoiceObservation) -> None:
        self.observation = observation
        self.started = time.monotonic()
        self.timings: dict[str, float] = {}
        self.reason = "empty"
        self.outcome = "skipped"
        self.timing_mode = "stages"

    @contextmanager
    def measure(self, stage: str):
        started = time.monotonic()
        try:
            yield
        finally:
            self.timings[stage] = round(
                self.timings.get(stage, 0.0) + (time.monotonic() - started) * 1000, 1,
            )

    def finish(self) -> None:
        self.observation.record(self.reason, self.outcome, started=self.started,
                                timings=self.timings, timing_mode=self.timing_mode)
