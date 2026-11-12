"""真实Live协议与事务上下文的来源、工具取消及权限边界。"""
from __future__ import annotations

import asyncio

from support.live_runtime import Socket, settle

from channels.models import RoomTarget, SessionRef
from context import create_context_service
from context.models import ContextRequest, Observation
from dialogue import create_gemini_backend
from dialogue.models import AudioInput, LiveSessionRequest, Reject
from operations.models import VoiceAgentSettings
from tests.support.context_fixtures import prepare_source


async def open_shared(tmp_path, *, authorize=False):
    context = create_context_service(str(tmp_path / "memory.jsonl"))
    await prepare_source(context.bridge, {"ok": True, "persona": "QQ persona", "provider": "memory_companion",
                         "memory_scopes": [{"umo": "qq:GroupMessage:123"}]})
    session = SessionRef("run", "binding", "session", RoomTarget("a", "c"))
    if authorize:
        context.observe(Observation("input", session, "capture", 0, "recognized", "请记住我喜欢茶", "user-a", "input"))
    prepared = await context.prepare_context(ContextRequest(session, "user-a", "input" if authorize else ""))
    socket = Socket()
    async def connect(*args, **kwargs):
        return socket
    settings = VoiceAgentSettings(gemini_api_key="synthetic", persona="local", voice_leave_enabled=True)
    backend = create_gemini_backend(settings, connector=connect)
    live = await backend.open_live(LiveSessionRequest(session, prepared), tools=context.tools(prepared))
    return context, prepared, socket, live


def test_live_setup_uses_astrbot_persona_and_retains_existing_voice_tools(tmp_path):
    async def run():
        context, _, socket, live = await open_shared(tmp_path)
        try:
            setup = socket.sent[0]["setup"]
            assert "QQ persona" in setup["system_instruction"]["parts"][0]["text"]
            names = {tool["name"] for tool in setup["tools"][0]["function_declarations"]}
            assert {"astrbot_recall", "astrbot_remember", "leave_voice_room"} <= names
        finally:
            await live.aclose()
            await context.close()
    asyncio.run(run())


def test_memory_rpc_does_not_block_live_receiver_and_repeated_tool_id_is_deduplicated(tmp_path):
    async def run():
        context, prepared, socket, live = await open_shared(tmp_path)
        events = live.events()
        try:
            await live.send_input(AudioInput("audio", prepared.context_ref.session, b"\0\x40" * 100, 16000, "user-a"))
            call = {"id": "tool-1", "name": "astrbot_recall", "args": {"query": "fact"}}
            await socket.deliver({"toolCall": {"functionCalls": [call]}})
            request = (await context.bridge.poll(wait=0))[0]
            assert request["payload"]["speaker"] == "user-a"
            # External RPC remains pending while the real socket reader accepts a transcription.
            await socket.deliver({"serverContent": {"inputTranscription": {"text": "还在听"}}})
            proposed = await asyncio.wait_for(anext(events), 1)
            if not proposed.text:
                proposed = await asyncio.wait_for(anext(events), 1)
            assert proposed.text == "还在听"
            context.bridge.resolve(request["request_id"], {"ok": True, "content": "memory result"})
            await settle()
            await socket.deliver({"toolCall": {"functionCalls": [call]}})
            responses = [message["toolResponse"]["functionResponses"][0] for message in socket.sent if "toolResponse" in message]
            assert len(responses) == 2
            assert responses[0]["response"] == responses[1]["response"] == {"ok": True, "content": "memory result"}
            assert context.bridge.status()["pending_requests"] == 0
        finally:
            await events.aclose()
            await live.aclose()
            await context.close()
    asyncio.run(run())


def test_live_emits_evidence_without_committing_unplayed_reply(tmp_path):
    async def run():
        context, prepared, socket, live = await open_shared(tmp_path)
        events = live.events()
        try:
            await live.send_input(AudioInput("audio", prepared.context_ref.session, b"\0\x40" * 100, 16000, "u"))
            await socket.deliver({"serverContent": {"inputTranscription": {"text": "hello"},
                                  "outputTranscription": {"text": "not played"}, "turnComplete": True}})
            event = await anext(events)
            while not event.text:
                event = await anext(events)
            assert event.text == "hello" and event.user_key == "u"
            await live.resolve_turn(event.proposal, Reject("policy"))
            assert context.history.as_messages() == []
            assert context.bridge.status()["pending_writes"] == 0
        finally:
            await events.aclose()
            await live.aclose()
            await context.close()
    asyncio.run(run())


def test_live_tool_cancellation_prevents_queued_memory_write(tmp_path):
    async def run():
        context, _, socket, live = await open_shared(tmp_path, authorize=True)
        try:
            call = {"id": "cancelled", "name": "astrbot_remember", "args": {"content": "never write"}}
            # Both external messages are already buffered: cancellation reaches the dispatcher
            # before its newly scheduled tool worker can begin the local transaction.
            await socket.queue.put(({"toolCall": {"functionCalls": [call]}}, None))
            await socket.queue.put(({"toolCallCancellation": {"ids": ["cancelled"]}}, None))
            await settle()
            assert context.bridge.status()["pending_requests"] == 0
            assert context.bridge.status()["pending_writes"] == 0
            assert context.delivery("tool-cancelled").state == "unknown"
        finally:
            await live.aclose()
            await context.close()
    asyncio.run(run())
