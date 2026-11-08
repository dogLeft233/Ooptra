"""有界耗时投影与逐句诊断适配；正文只提交上下文的独立诊断表。"""
from __future__ import annotations

import copy
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Literal

from channels.models import SessionRef
from context.api import ContextService
from context.models import VoiceTrace

logger = logging.getLogger(__name__)


class FlowObservations:
    def __init__(self, limit: int = 40, *, journal: ContextService | None = None):
        self._rows = deque(maxlen=limit)
        self._journal = journal

    def trace(self, session: SessionRef, input_ref: str, user_key: str,
              stage: Literal["recognizing", "recognized", "deciding", "finished"], *,
              text: str = "", text_final: bool = True, decision: str = "",
              reason: str = "", outcome: str = "", elapsed_ms: float = 0,
              recognition_status: Literal["", "pending", "recognized", "no_speech", "failed", "interrupted"] = "") -> None:
        if self._journal is None:
            return
        try:
            self._journal.record_voice_trace(VoiceTrace(session, input_ref, user_key, stage,
                text[:65536], text_final, decision, reason, outcome, max(0, elapsed_ms),
                recognition_status=recognition_status or ("pending" if stage == "recognizing" else "")))
        except Exception:
            logger.exception("event=voice.trace.failed session_id=%s input_ref=%s",
                             session.session_id, input_ref)

    def record(self, *, trigger_id: str, session_id: str, backend: str, reason: str,
               outcome: str, elapsed_ms: float, timings_ms=None, timing_mode="stages") -> None:
        timings = timings_ms or {}
        self._rows.append(dict(timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            trigger_id=trigger_id, session_id=session_id, backend=backend, reason=reason,
            outcome=outcome, elapsed_ms=round(max(0, elapsed_ms), 1), timing_mode=timing_mode,
            timings_ms={stage:timings.get(stage) for stage in ("asr", "asr_finalize", "intent", "chat", "tts", "generation_total")}))

    def snapshot(self):
        return {"recent_decisions": copy.deepcopy(list(self._rows)), "limit": self._rows.maxlen}
