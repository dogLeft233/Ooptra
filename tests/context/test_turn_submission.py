"""一次提交共享实际持久化结果，取消与失败不触发第二次受理。"""
import asyncio
import json
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


@pytest.mark.parametrize("external_fail,local_fail", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("cancelled", [False, True])
def test_independent_storage_failures_are_observed_once(tmp_path, monkeypatch, external_fail, local_fail, cancelled):
    async def run():
        history_path, outbox_path = tmp_path / "history.jsonl", tmp_path / "outbox.json"
        history = create_memory(str(history_path))
        bridge = create_context_bridge(outbox_path=outbox_path)
        await prepare_source(bridge, {"ok": True, "provider": "memory_companion",
            "memory_scopes": [{"umo": "original"}]})
        if external_fail:
            outbox_path.unlink(missing_ok=True)
            outbox_path.mkdir()
        if local_fail:
            history_path.mkdir()
        entered, released = threading.Event(), threading.Event()
        original = history.record
        def blocked_real_write(turn):
            entered.set()
            assert released.wait(3)
            return original(turn)
        monkeypatch.setattr(history, "record", blocked_real_write)
        submission = create_turn_submission(history, bridge, speaker="member")
        pending = asyncio.create_task(submission.record(ConfirmedTurn("question", "answer", "completed")))
        try:
            assert await asyncio.to_thread(entered.wait, 1), "external failure must still attempt local storage"
            if cancelled:
                pending.cancel()
            released.set()
            with pytest.raises(OSError) as failure:
                await pending
            if local_fail:
                assert failure.value.filename == str(history_path)
                assert history_path.is_dir()
            else:
                assert history.as_messages() == [
                    {"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}]
            if external_fail and local_fail:
                assert isinstance(failure.value.__cause__, OSError)
                assert failure.value.__cause__.filename2 == str(outbox_path)
            elif external_fail:
                assert failure.value.filename2 == str(outbox_path)
            assert bridge.status()["pending_writes"] == (0 if external_fail else 1)
            persisted_outbox = None
            if not external_fail:
                persisted_outbox = outbox_path.read_text(encoding="utf-8")
                commits = [event for event in json.loads(persisted_outbox) if event["operation"] == "commit"]
                assert len(commits) == 1
                assert commits[0]["payload"]["reply_text"] == "answer"
            with pytest.raises(OSError) as repeated:
                await submission.record(ConfirmedTurn("question", "", "interrupted"))
            assert repeated.value is failure.value
            if persisted_outbox is not None:
                assert outbox_path.read_text(encoding="utf-8") == persisted_outbox
            if not local_fail:
                assert len(history.recent()) == 2
        finally:
            released.set()
            if not pending.done():
                with pytest.raises(OSError):
                    await pending
    asyncio.run(run())
