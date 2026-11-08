import asyncio

import pytest

from channels.models import ActionOutcome, RoomTarget, SessionRef
from context import create_context_service
from context.models import ConfirmedTurn, ContextRequest, Observation, OutputRecord


def session(name="s1"):
    return SessionRef("run", "binding", name, RoomTarget("area", "room"))


def test_output_and_delivery_survive_restart_without_duplicate_history(tmp_path):
    async def scenario():
        path = str(tmp_path / "voice_memory.jsonl")
        service = create_context_service(path)
        ref = session()
        service.observe(Observation("input1", ref, "connection", 1, "recognized", "hello", "u", "input1"))
        prepared = await service.prepare_context(ContextRequest(ref, user_key="u", input_ref="input1"))
        record = OutputRecord("action1", ref, ConfirmedTurn("hello", "hi", "completed", "u", "room"),
                              ActionOutcome("action1", "completed", ref), prepared.context_ref, "input1")
        first = service.record_output(record)
        second = service.record_output(record)
        assert first == second
        assert service.history.as_messages() == [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        await service.close()
        restored = create_context_service(path)
        assert restored.record_output(record) == first
        assert restored.history.as_messages() == [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        await restored.close()
    asyncio.run(scenario())


def test_context_rejects_changed_action_and_preserves_original_source_delivery(tmp_path):
    async def scenario():
        service = create_context_service(str(tmp_path / "memory.jsonl"))
        ref = session()
        poll_task = asyncio.create_task(service.bridge.poll(wait=0, client_id="worker"))
        await poll_task
        preparing = asyncio.create_task(service.bridge.prepare("area", "room"))
        await asyncio.sleep(0)
        events = await service.bridge.poll(wait=0, client_id="worker")
        service.bridge.resolve(events[0]["request_id"], {"ok": True, "persona": "p", "provider": "provider", "memory_scopes": [{"umo": "original"}]})
        await preparing
        prepared = await service.prepare_context(ContextRequest(ref))
        record = OutputRecord("a", ref, ConfirmedTurn("hello", "hi", "completed", "u", "room"), ActionOutcome("a", "completed", ref), prepared.context_ref)
        receipt = service.record_output(record)
        assert receipt.delivery_admission == "accepted"
        await asyncio.sleep(0)
        service.bridge.clear()
        events = await service.bridge.poll(wait=0, client_id="worker")
        assert events[0]["scope"]["memory_scopes"] == [{"umo": "original"}]
        assert events[0]["session_id"] == prepared.context_ref.binding_id
        changed = OutputRecord("a", ref, ConfirmedTurn("hello", "changed", "completed", "u", "room"), ActionOutcome("a", "completed", ref), prepared.context_ref)
        with pytest.raises(ValueError, match="冲突"):
            service.record_output(changed)
        service.bridge.resolve(events[0]["request_id"], {"ok": True, "state": "stored"})
        assert service.delivery(receipt.delivery_id).state == "confirmed"
        await service.close()
        restored = create_context_service(str(tmp_path / "memory.jsonl"))
        assert restored.delivery(receipt.delivery_id).state == "confirmed"
        assert await restored.bridge.poll(wait=0, client_id="worker") == []
        await restored.close()
    asyncio.run(scenario())


def test_observation_keeps_new_session_projection_and_room_history_isolated(tmp_path):
    async def scenario():
        service = create_context_service(str(tmp_path / "memory.jsonl"))
        old, new = session("old"), session("new")
        service.observe(Observation("new", new, "connection", 1, "session"))
        service.observe(Observation("late", old, "connection", 2, "recognized", "old text", "u", "late"))
        assert service.snapshot(new.room_target).session == new
        elsewhere = SessionRef("run", "binding", "elsewhere", RoomTarget("area", "other"))
        service.observe(Observation("other", elsewhere, "connection", 3, "recognized", "secret", "u", "other"))
        prepared = await service.prepare_context(ContextRequest(new))
        assert [row.content for row in prepared.history] == ["old text"]
        assert service.observe(Observation("new", new, "connection", 1, "session")) == service.snapshot(new.room_target).revision
        await service.close()
    asyncio.run(scenario())


def test_recognized_input_retains_members_and_gap_marks_snapshot_incomplete(tmp_path):
    service = create_context_service(str(tmp_path / "history.jsonl"))
    ref = session()
    service.observe(Observation("members", ref, "connection", 1, "members", members=("u", "v")))
    service.observe(Observation("input", ref, "connection", 2, "recognized", "hello", "u", "input"))
    snapshot = service.snapshot(ref.room_target)
    assert snapshot.members == ("u", "v")
    service.observe(Observation("gap", ref, "connection", 4, "activity"))
    assert service.snapshot(ref.room_target).complete is False


def test_memory_tool_rejects_malformed_content_without_crashing(tmp_path):
    async def scenario():
        service = create_context_service(str(tmp_path / "history.jsonl"))
        await service.bridge.poll(wait=0, client_id="worker")
        pending = asyncio.create_task(service.bridge.prepare("area", "room"))
        await asyncio.sleep(0)
        event = (await service.bridge.poll(wait=0, client_id="worker"))[0]
        service.bridge.resolve(event["request_id"], {"ok": True, "provider": "p", "memory_scopes": [{"umo": "room"}]})
        await pending
        ref = session()
        service.observe(Observation("i", ref, "connection", 1, "recognized", "请记住我的偏好", "u", "i"))
        prepared = await service.prepare_context(ContextRequest(ref, input_ref="i"))
        tools = service.tools(prepared)
        assert (await tools.tool("astrbot_remember", {"content": 123}))["ok"] is False
        assert (await tools.tool("astrbot_recall", {"query": []}))["ok"] is False
        await service.close()
    asyncio.run(scenario())


def test_partial_delivery_only_retries_unconfirmed_targets_after_restart(tmp_path):
    import json

    history = tmp_path / "history.jsonl"
    outbox = tmp_path / "outbox.json"
    outbox.write_text(json.dumps([{"protocol": 1, "request_id": "r", "session_id": "s", "operation": "remember",
                                  "room": {"area": "a", "channel": "c"},
                                  "scope": {"memory_scopes": [{"umo": "one"}, {"umo": "two"}]},
                                  "payload": {"content": "fact"}}]))
    async def scenario():
        service = create_context_service(str(history), outbox_path=outbox)
        await service.bridge.poll(wait=0, client_id="worker")
        service.bridge.resolve("r", {"ok": False, "retry": True, "targets": [{"umo": "one", "ok": True}, {"umo": "two", "ok": False}]})
        await service.close()
        service = create_context_service(str(history), outbox_path=outbox)
        retry = (await service.bridge.poll(wait=0, client_id="worker"))[0]
        assert retry["request_id"] == "r"
        assert retry["scope"]["memory_scopes"] == [{"umo": "two"}]
        await service.close()
    asyncio.run(scenario())


def test_transaction_failure_leaves_neither_history_nor_delivery(tmp_path, monkeypatch):
    import sqlite3

    async def scenario():
        service = create_context_service(str(tmp_path / "history.jsonl"))
        await service.bridge.poll(wait=0, client_id="worker")
        preparing = asyncio.create_task(service.bridge.prepare("area", "room"))
        await asyncio.sleep(0)
        event = (await service.bridge.poll(wait=0, client_id="worker"))[0]
        service.bridge.resolve(event["request_id"], {"ok": True, "provider": "p", "memory_scopes": [{"umo": "room"}]})
        await preparing
        ref = session()
        prepared = await service.prepare_context(ContextRequest(ref))
        record = OutputRecord("atomic", ref, ConfirmedTurn("question", "answer", "completed"), ActionOutcome("atomic", "completed", ref), prepared.context_ref)
        connect = sqlite3.connect
        def deny_delivery(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_authorizer(lambda operation, table, *_: sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_INSERT and table == "deliveries" else sqlite3.SQLITE_OK)
            return db
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", deny_delivery)
            with pytest.raises(sqlite3.DatabaseError):
                service.record_output(record)
        assert service.history.as_messages() == []
        assert service.delivery("record-atomic").state == "unknown"
        assert service.record_output(record).delivery_admission == "accepted"
        await service.close()
    asyncio.run(scenario())


def test_write_authorization_is_registered_and_revoked_with_source(tmp_path):
    from dataclasses import replace

    from context.models import RememberRequest

    async def scenario():
        service = create_context_service(str(tmp_path / "history.jsonl"))
        await service.bridge.poll(wait=0, client_id="worker")
        preparing = asyncio.create_task(service.bridge.prepare("area", "room"))
        await asyncio.sleep(0)
        event = (await service.bridge.poll(wait=0, client_id="worker"))[0]
        service.bridge.resolve(event["request_id"], {"ok": True, "provider": "p", "memory_scopes": [{"umo": "room"}]})
        await preparing
        ref = session()
        service.observe(Observation("partial", ref, "connection", 1, "recognized", "请记住我", "u", "input", complete=False))
        assert service.history.as_messages() == []
        assert not (await service.prepare_context(ContextRequest(ref, input_ref="input"))).authorized_write_grants
        service.observe(Observation("final", ref, "connection", 2, "recognized", "请记住我喜欢茶", "u", "input"))
        prepared = await service.prepare_context(ContextRequest(ref, input_ref="input"))
        grant = prepared.authorized_write_grants[0]
        with pytest.raises(ValueError, match="授权"):
            await service.remember(RememberRequest("call", prepared.context_ref, "likes tea", replace(grant, grant_id="forged")))
        first = await service.remember(RememberRequest("call", prepared.context_ref, "likes tea", grant))
        assert first["queued"] is True and first["ok"] is False
        assert await service.remember(RememberRequest("call", prepared.context_ref, "likes tea", grant)) == first
        service.bridge.clear()
        with pytest.raises(ValueError, match="失效"):
            await service.remember(RememberRequest("other", prepared.context_ref, "likes tea", grant))
        await service.close()
    asyncio.run(scenario())


def test_legacy_import_and_export_keep_original_files_and_request_id(tmp_path):
    import json

    history = tmp_path / "memory.jsonl"
    original = '{"role":"user","content":"old"}\n'
    history.write_text(original)
    outbox = tmp_path / "outbox.json"
    event = {"protocol": 1, "request_id": "original", "session_id": "s", "operation": "remember", "room": {}, "scope": {}, "payload": {"content": "fact"}}
    outbox.write_text(json.dumps([event]))
    async def scenario():
        service = create_context_service(str(history), outbox_path=outbox)
        service.history.append("assistant", "new")
        exported = await service.export_legacy(tmp_path / "export")
        assert exported.history_count == 2 and exported.delivery_count == 1
        assert history.read_text() == original
        assert json.loads((tmp_path / "export" / "astrbot_voice_outbox.json").read_text())[0]["request_id"] == "original"
        await service.close()
        restored = create_context_service(str(history), outbox_path=outbox)
        assert [row["content"] for row in restored.history.recent()] == ["old", "new"]
        await restored.close()
    asyncio.run(scenario())


def test_full_outbox_can_admit_same_record_later_without_repeating_history(tmp_path):
    import json

    history = tmp_path / "memory.jsonl"
    outbox = tmp_path / "outbox.json"
    outbox.write_text(json.dumps([{"protocol": 1, "request_id": f"pending-{index}", "session_id": "s", "operation": "remember", "room": {}, "scope": {}, "payload": {"content": "fact"}} for index in range(128)]))
    async def scenario():
        service = create_context_service(str(history), outbox_path=outbox)
        await service.bridge.poll(wait=0, client_id="worker")
        preparing = asyncio.create_task(service.bridge.prepare("area", "room"))
        await asyncio.sleep(0)
        batch = await service.bridge.poll(wait=0, client_id="worker")
        event = next(row for row in batch if row["operation"] == "prepare")
        service.bridge.resolve(event["request_id"], {"ok": True, "provider": "p", "memory_scopes": [{"umo": "original"}]})
        await preparing
        ref = session()
        prepared = await service.prepare_context(ContextRequest(ref))
        record = OutputRecord("capacity", ref, ConfirmedTurn("hello", "hi", "completed"), ActionOutcome("capacity", "completed", ref), prepared.context_ref)
        first = service.record_output(record)
        assert first.delivery_admission == "rejected_capacity" and first.delivery_id is None
        service.bridge.resolve("pending-0", {"ok": True})
        retry = service.record_output(record)
        assert retry.delivery_id == "record-capacity"
        assert [row["content"] for row in service.history.recent()] == ["hello", "hi"]
        await service.close()
    asyncio.run(scenario())


def test_typed_rpc_rejects_wrong_peer_and_recognizes_duplicate_receipt(tmp_path):
    import json

    from context.models import PollRequest, RpcReceipt

    outbox = tmp_path / "outbox.json"
    outbox.write_text(json.dumps([{"protocol": 1, "request_id": "r", "session_id": "s", "operation": "remember", "room": {}, "scope": {}, "payload": {"content": "fact"}}]))
    async def scenario():
        service = create_context_service(str(tmp_path / "history.jsonl"), outbox_path=outbox)
        assert (await service.bridge.poll_request(PollRequest("owner", 0))).status == "events"
        assert (await service.bridge.poll_request(PollRequest("other", 0))).status == "peer_conflict"
        wrong = RpcReceipt.parse({"request_id": "r", "client_id": "other", "result": {"ok": True}})
        assert service.bridge.resolve_receipt(wrong).status == "rejected"
        right = RpcReceipt.parse({"request_id": "r", "client_id": "owner", "result": {"ok": True}})
        assert service.bridge.resolve_receipt(right).status == "accepted"
        assert service.bridge.resolve_receipt(right).status == "duplicate"
        await service.close()
        assert (await service.bridge.poll_request(PollRequest("owner", 0))).status == "closed"
    asyncio.run(scenario())


def test_preparation_excludes_current_input_by_identity_without_removing_equal_old_text(tmp_path):
    async def run():
        context = create_context_service(str(tmp_path / "memory.jsonl"))
        ref = session()
        try:
            context.observe(Observation("old", ref, "capture", 1, "recognized", "same question", "u", "old"))
            context.observe(Observation("current", ref, "capture", 2, "recognized", "same question", "u", "current"))
            prepared = await context.prepare_context(ContextRequest(ref, "u", "current"))
            assert [row.content for row in prepared.history] == ["same question"]
            assert [row["content"] for row in context.history.recent()] == ["same question", "same question"]
            assert [row.content for row in context.snapshot(ref.room_target).history] == ["same question", "same question"]
        finally:
            await context.close()
    asyncio.run(run())
