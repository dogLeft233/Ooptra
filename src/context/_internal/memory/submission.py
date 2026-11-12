"""一次上下文提交的异步生命周期，不让取消重新受理同一事实。"""
from __future__ import annotations

import asyncio

from context.api import ConversationHistory, TurnRecorder
from context.models import ConfirmedTurn, RecordReceipt


class TurnSubmission:
    def __init__(self, history: ConversationHistory | None, recorder: TurnRecorder) -> None:
        self._history, self._recorder = history, recorder
        self._task: asyncio.Task[RecordReceipt] | None = None

    async def _persist(self, turn: ConfirmedTurn) -> RecordReceipt:
        external_failure: Exception | None = None
        try:
            self._recorder(turn.user_text, turn.reply_text,
                           interrupted=turn.confirmation in {"interrupted", "failed", "unknown"})
        except Exception as exc:
            external_failure = exc
        try:
            receipt = (await asyncio.to_thread(self._history.record, turn)
                       if self._history is not None else RecordReceipt(False, False))
        except Exception as exc:
            if external_failure is not None:
                raise exc from external_failure
            raise
        if external_failure is not None:
            raise external_failure
        return receipt

    async def record(self, turn: ConfirmedTurn) -> RecordReceipt:
        if self._task is None:
            self._task = asyncio.create_task(self._persist(turn), name="context-turn-submit")
        task = self._task
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        receipt = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return receipt
