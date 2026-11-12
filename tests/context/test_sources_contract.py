import asyncio
import contextlib

from context import create_context_bridge
from tests.support.context_fixtures import prepare_source


def test_prepared_source_is_isolated_from_worker_mutation():
    async def run():
        bridge = create_context_bridge()
        result = {"ok": True, "persona": "original", "memory_scopes": [{"umo": "first"}]}
        await prepare_source(bridge, result)
        result["persona"] = "tampered"
        result["memory_scopes"][0]["umo"] = "other"
        assert bridge.persona == "original"
        assert bridge.context["memory_scopes"] == [{"umo": "first"}]
    asyncio.run(run())


def test_finished_prepare_cannot_reinstall_source_after_clear():
    async def run():
        bridge = create_context_bridge()
        await bridge.poll(wait=0)
        task = asyncio.create_task(bridge.prepare("old", "room"))
        await asyncio.sleep(0)
        event = (await bridge.poll(wait=0))[0]
        bridge.resolve(event["request_id"], {"ok": True, "persona": "old"})
        bridge.clear()
        await task
        assert not bridge.active
        assert bridge.persona == ""
    asyncio.run(run())


def test_poll_event_and_snapshot_cannot_redirect_queued_memory(tmp_path):
    async def run():
        path = tmp_path / "outbox.json"
        bridge = create_context_bridge(outbox_path=path)
        await prepare_source(bridge, {"ok": True, "provider": "memory_companion",
                                     "memory_scopes": [{"umo": "original"}]})
        bridge.record_turn("question", "answer", speaker="u")
        event = (await bridge.poll(wait=0))[0]
        event["scope"]["memory_scopes"][0]["umo"] = "hijacked"
        snapshot = bridge.context
        snapshot["memory_scopes"][0]["umo"] = "changed"
        restored = create_context_bridge(outbox_path=path)
        retry = (await restored.poll(wait=0))[0]
        assert retry["scope"]["memory_scopes"] == [{"umo": "original"}]
    asyncio.run(run())


def test_cancelled_remember_preserves_original_request_for_retry(tmp_path):
    async def run():
        path = tmp_path / "outbox.json"
        bridge = create_context_bridge(outbox_path=path)
        await prepare_source(bridge, {"ok": True, "provider": "memory_companion"})
        task = asyncio.create_task(bridge.tool("astrbot_remember", {"content": "fact"}))
        await asyncio.sleep(0)
        event = (await bridge.poll(wait=0))[0]
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        restored = create_context_bridge(outbox_path=path)
        pending = await restored.poll(wait=0)
        assert len(pending) == 1
        assert pending[0]["request_id"] == event["request_id"]
        assert pending[0]["payload"]["content"] == "fact"
    asyncio.run(run())


def test_disk_failure_does_not_publish_or_confirm_queued_turn(tmp_path, monkeypatch):
    import os

    import pytest

    async def run():
        bridge = create_context_bridge(outbox_path=tmp_path / "outbox.json")
        await prepare_source(bridge, {"ok": True, "provider": "memory_companion"})
        def disk_full(*_args):
            raise OSError("disk full")
        monkeypatch.setattr(os, "replace", disk_full)
        with pytest.raises(OSError, match="disk full"):
            bridge.record_turn("question", "answer", speaker="u")
        assert await bridge.poll(wait=0) == []
        assert bridge.history == []
        assert bridge.status()["pending_writes"] == 0
    asyncio.run(run())


def test_invalid_existing_outbox_is_never_treated_as_empty(tmp_path):
    import pytest

    path = tmp_path / "outbox.json"
    path.write_text('[{"operation": "commit"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="待写队列"):
        create_context_bridge(outbox_path=path)
    assert path.read_text(encoding="utf-8") == '[{"operation": "commit"}]'
