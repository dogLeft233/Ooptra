from __future__ import annotations

import asyncio
import json

from voice_agent.context_bridge import ContextBridge


def test_persona_only_sources_do_not_queue_memory_writes():
    bridge = ContextBridge()
    bridge.context = {"ok": True, "umo": "Bot:GroupMessage:123", "memory_scopes": [], "provider": ""}
    bridge.record_turn("hello", "hi", speaker="user")
    assert not bridge.outbox


def test_partial_explicit_remember_retries_durably_with_same_request_id(tmp_path):
    async def run():
        path = tmp_path / "outbox.json"
        bridge = ContextBridge(outbox_path=path)
        bridge.context = {"ok": True, "provider": "memory_companion"}
        await bridge.poll(wait=0)
        task = asyncio.create_task(bridge.tool("astrbot_remember", {"content": "shared fact"}))
        await asyncio.sleep(0)
        event = (await bridge.poll(wait=0))[0]
        bridge.resolve(event["request_id"], {"ok": False, "retry": True, "state": "partial"})
        result = await task
        assert result["ok"] is False and result["queued"] is True
        restored = ContextBridge(outbox_path=path)
        retry = (await restored.poll(wait=0))[0]
        assert retry["request_id"] == event["request_id"]
        assert retry["operation"] == "remember"
        restored.resolve(retry["request_id"], {"ok": True, "state": "stored"})
        assert not restored.outbox
    asyncio.run(run())


def test_pending_remember_reserves_retry_capacity_before_any_target_is_written():
    async def run():
        bridge = ContextBridge()
        bridge.context = {"ok": True, "provider": "memory_companion"}
        await bridge.poll(wait=0)
        bridge.outbox = {str(n): {} for n in range(127)}
        first = asyncio.create_task(bridge.tool("astrbot_remember", {"content": "first"}))
        await asyncio.sleep(0)
        rejected = await bridge.tool("astrbot_remember", {"content": "second"})
        assert rejected["ok"] is False
        assert len(bridge.pending) == 1
        bridge.record_turn("ordinary turn", "reply", speaker="user")
        assert len(bridge.outbox) == 127
        request_id = next(iter(bridge.pending))
        bridge.resolve(request_id, {"ok": False, "retry": True})
        assert (await first)["queued"] is True
        assert len(bridge.outbox) == 128
    asyncio.run(run())


def test_outbox_preserves_all_selected_memory_scopes_and_partial_retry(tmp_path):
    async def run():
        bridge = ContextBridge(outbox_path=tmp_path / "outbox.json")
        scopes = [{"umo": "Bot:GroupMessage:123"}, {"umo": "Bot:FriendMessage:456"}]
        bridge.context = {"ok": True, "umo": scopes[0]["umo"], "memory_scopes": scopes,
                          "memory_route_id": "original-rule", "provider": "memory_companion"}
        bridge.record_turn("shared fact", "okay", speaker="user")
        event = (await bridge.poll(wait=0))[0]
        assert event["scope"]["memory_scopes"] == scopes
        bridge.context = {"ok": True, "umo": "Other:GroupMessage:999"}
        assert bridge.resolve(event["request_id"], {"ok": False, "retry": True,
            "targets": [{"umo": scopes[0]["umo"], "ok": True}, {"umo": scopes[1]["umo"], "ok": False}]})
        restored = ContextBridge(outbox_path=tmp_path / "outbox.json")
        retried = (await restored.poll(wait=0))[0]
        assert retried["request_id"] == event["request_id"]
        assert retried["scope"]["memory_scopes"] == scopes
        assert retried["scope"]["memory_route_id"] == "original-rule"
    asyncio.run(run())


def test_prepare_uses_correlated_reply_and_drops_stale_results():
    async def run():
        bridge = ContextBridge(timeout=0.1)
        await bridge.poll(wait=0)
        task = asyncio.create_task(bridge.prepare("area", "room"))
        await asyncio.sleep(0)
        request = (await bridge.poll(wait=0))[0]
        assert request["operation"] == "prepare"
        assert request["room"] == {"area": "area", "channel": "room"}
        assert not bridge.resolve("wrong", {"ok": True})
        bridge.resolve(request["request_id"], {
            "ok": True, "persona": "AstrBot persona", "group_id": "123", "umo": "qq:GroupMessage:123",
        })
        await task
        assert bridge.persona == "AstrBot persona"
        old_session = bridge.session_id
        bridge.clear()
        assert bridge.session_id != old_session
        assert not bridge.active
        assert not bridge.resolve(request["request_id"], {"ok": True})
    asyncio.run(run())


def test_outbox_survives_restart_and_acknowledges_only_matching_request(tmp_path):
    async def run():
        path = tmp_path / "outbox.json"
        bridge = ContextBridge(outbox_path=path)
        bridge.context = {"ok": True, "group_id": "123", "umo": "qq:GroupMessage:123"}
        bridge.room = {"area": "a", "channel": "c"}
        bridge.record_turn("hello", "hi", speaker="oopz-user", interrupted=False)
        restored = ContextBridge(outbox_path=path)
        event = (await restored.poll(wait=0))[0]
        assert event["operation"] == "commit"
        assert event["payload"]["user_text"] == "hello"
        assert event["scope"]["group_id"] == "123"
        assert restored.resolve(event["request_id"], {"ok": True})
        assert json.loads(path.read_text(encoding="utf-8")) == []
        assert not restored.resolve(event["request_id"], {"ok": True})
    asyncio.run(run())


def test_offline_scope_never_uses_last_rooms_persona():
    async def run():
        bridge = ContextBridge(timeout=0.01)
        bridge.context = {"ok": True, "persona": "old"}
        await bridge.prepare("other", "room")
        assert bridge.persona == ""
        assert not bridge.active
        assert bridge.status()["state"] == "offline"
    asyncio.run(run())


def test_interrupted_reply_is_not_written_as_complete_assistant_turn():
    bridge = ContextBridge()
    bridge.context = {"ok": True, "umo": "qq:GroupMessage:123"}
    bridge.record_turn("question", "unfinished", speaker="", interrupted=True)
    event = next(iter(bridge.outbox.values()))
    assert event["payload"]["reply_text"] == ""
    assert event["payload"]["interrupted"] is True


def test_binding_management_survives_voice_room_switch():
    async def run():
        bridge = ContextBridge(timeout=1)
        await bridge.poll(wait=0)
        task = asyncio.create_task(bridge.request("bindings", {"action": "save"}))
        await asyncio.sleep(0)
        event = (await bridge.poll(wait=0))[0]
        bridge._leased[event["request_id"]] -= 9
        assert await bridge.poll(wait=0) == [], "slow configuration writes must not be delivered twice"
        bridge.clear()
        assert bridge.resolve(event["request_id"], {"ok": True, "bindings": []})
        assert (await task)["ok"] is True
    asyncio.run(run())


def test_bound_turn_preserves_original_scope_and_never_updates_new_session(tmp_path):
    bridge = ContextBridge(outbox_path=tmp_path / "outbox.json")
    bridge.context = {"ok": True, "provider": "memory_companion",
                      "memory_scopes": [{"umo": "original"}]}
    bridge.room = {"area": "original-area", "channel": "original-room"}
    original_session = bridge.session_id
    record = bridge.bind_turn(speaker="original-member")
    bridge.context["memory_scopes"][0]["umo"] = "mutated"
    bridge.clear()
    bridge.context = {"ok": True, "provider": "other-provider"}
    record("你好", "未完成回答", interrupted=True)
    record("你好", "未完成回答", interrupted=True)
    event = next(iter(bridge.outbox.values()))
    assert len(bridge.outbox) == 1
    assert event["session_id"] == original_session
    assert event["scope"]["memory_scopes"] == [{"umo": "original"}]
    assert event["room"] == {"area": "original-area", "channel": "original-room"}
    assert event["payload"] == {"user_text": "你好", "reply_text": "",
                                "speaker": "original-member", "interrupted": True}
    assert bridge.history == []
    assert len(ContextBridge(outbox_path=tmp_path / "outbox.json").outbox) == 1
