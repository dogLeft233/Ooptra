"""流程拥有的有界诊断元数据；不保存转写、回复、凭据或供应商异常正文。"""
from __future__ import annotations

import copy
from collections import deque
from datetime import datetime, timezone


class FlowObservations:
    def __init__(self, limit: int = 40):
        self._rows = deque(maxlen=limit)

    def record(self, *, trigger_id: str, session_id: str, backend: str, reason: str,
               outcome: str, elapsed_ms: float, timings_ms=None, timing_mode="stages") -> None:
        timings = timings_ms or {}
        self._rows.append(dict(timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            trigger_id=trigger_id, session_id=session_id, backend=backend, reason=reason,
            outcome=outcome, elapsed_ms=round(max(0, elapsed_ms), 1), timing_mode=timing_mode,
            timings_ms={stage:timings.get(stage) for stage in ("asr", "asr_finalize", "intent", "chat", "tts", "generation_total")}))

    def snapshot(self):
        return {"recent_decisions": copy.deepcopy(list(self._rows)), "limit": self._rows.maxlen}
