"""一次提交共享实际持久化结果，取消与失败不触发第二次受理。"""
import asyncio
import threading

import pytest
from support.context_fixtures import prepare_source

from context import create_context_bridge, create_memory, create_turn_submission
from context.models import ConfirmedTurn


def test_cancelled_failed_write_remains_observable_and_does_not_retry(tmp_path, monkeypatch):
    async def run():
        history = create_memory(str(tmp_path / "history.jsonl"))
        bridge = create_context_bridge(outbox_path=tmp_path / "outbox.json")
        await prepare_source(bridge, {"ok": True, "provider": "memory_companion",
            "memory_scopes": [{"umo": "original"}]})
        entered, released = threading.Event(), threading.Event()
        def fail_write(text):
            entered.set()
            assert released.wait(3)
            raise OSError("synthetic disk failure")
        monkeypatch.setattr(history, "_append_lines", fail_write)
        submission = create_turn_submission(history, bridge, speaker="member")
        turn = ConfirmedTurn("question", "answer", "completed")
        pending = asyncio.create_task(submission.record(turn))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            pending.cancel()
            await asyncio.sleep(0)
            released.set()
            with pytest.raises(OSError, match="synthetic disk failure"):
                await pending
            with pytest.raises(OSError, match="synthetic disk failure"):
                await submission.record(ConfirmedTurn("question", "", "interrupted"))
            assert history.as_messages() == []
            assert bridge.status()["pending_writes"] == 1
        finally:
            released.set()
    asyncio.run(run())
