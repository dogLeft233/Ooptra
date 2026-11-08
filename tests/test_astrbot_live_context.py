from __future__ import annotations

import asyncio
import json

from support.dialogue_fixtures import gemini_backend as GeminiLiveBackend

from context import create_context_bridge as ContextBridge
from operations.models import VoiceAgentSettings
from tests.support.context_fixtures import prepare_source


class Memory:
    def append(self, *args, **kwargs):
        pass


def test_live_setup_uses_astrbot_persona_and_retains_existing_voice_tools():
    async def run():
        bridge = ContextBridge()
        await bridge.poll(wait=0)
        await prepare_source(bridge, {"ok": True, "persona": "QQ persona", "provider": "memory_companion"})
        backend = GeminiLiveBackend(VoiceAgentSettings(persona="local"), Memory())
        backend.context_bridge = bridge
        sent = []

        async def send(value):
            sent.append(value)

        backend._send = send
        await backend._send_setup()
        setup = sent[0]["setup"]
        assert "QQ persona" in setup["system_instruction"]["parts"][0]["text"]
        names = {tool["name"] for tool in setup["tools"][0]["function_declarations"]}
        assert {"astrbot_recall", "astrbot_remember", "leave_voice_room"} <= names
    asyncio.run(run())


def test_memory_rpc_does_not_block_live_receiver_and_repeated_tool_id_is_deduplicated():
    async def run():
        bridge = ContextBridge(timeout=1)
        await bridge.poll(wait=0)
        await prepare_source(bridge, {"ok": True, "provider": "memory_companion"})
        backend = GeminiLiveBackend(VoiceAgentSettings(reply_probability_percent=100), Memory())
        backend.context_bridge = bridge
        backend.note_input_speaker("user-a")
        sent = []

        async def send(value):
            sent.append(value)

        backend._send = send
        call = {"id": "tool-1", "name": "astrbot_remember", "args": {"content": "fact"}}
        await asyncio.wait_for(backend._handle_server_msg({"toolCall": {"functionCalls": [call]}}), .1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        request = (await bridge.poll(wait=0))[0]
        assert request["payload"]["speaker"] == "user-a"
        # Still receiving transcription while memory is pending.
        await backend._handle_server_msg({"serverContent": {"inputTranscription": {"text": "记住 fact"}}})
        assert backend._last_user_text == "记住 fact"
        bridge.resolve(request["request_id"], {"ok": True, "state": "stored"})
        await asyncio.gather(*backend._memory_tasks)
        await backend._handle_server_msg({"toolCall": {"functionCalls": [call]}})
        await asyncio.gather(*backend._memory_tasks)
        assert len(sent) == 2
        assert bridge.status()["pending_requests"] == 0
        assert sent[-1]["toolResponse"]["functionResponses"][0]["response"]["ok"] is True
    asyncio.run(run())


def test_live_emits_evidence_without_committing_unplayed_reply():
    async def run():
        bridge = ContextBridge()
        await bridge.poll(wait=0)
        await prepare_source(bridge, {"ok": True, "umo": "qq:GroupMessage:123"})
        backend = GeminiLiveBackend(VoiceAgentSettings(reply_probability_percent=0), Memory())
        backend.context_bridge = bridge
        results = []
        backend.set_generated_out(results.append)
        backend.note_input_speaker("u")
        await backend._handle_server_msg({"serverContent": {
            "inputTranscription": {"text": "hello"},
            "outputTranscription": {"text": "not played"}, "turnComplete": True}})
        turn = results[0]
        assert turn.user_text == "hello"
        assert turn.text == "not played"
        assert turn.selected is False
        assert turn.user_key == "u"
        assert bridge.status()["pending_writes"] == 0
        backend._announcement = True
        backend._last_user_text = "internal instruction"
        backend._last_reply = "announcement"
        await backend._handle_server_msg({"serverContent": {"turnComplete": True}})
        assert results[-1].internal is True
        assert bridge.status()["pending_writes"] == 0
        assert "internal instruction" not in json.dumps(bridge.history)
    asyncio.run(run())


def test_live_tool_cancellation_prevents_queued_memory_write():
    async def run():
        bridge = ContextBridge(timeout=1)
        await bridge.poll(wait=0)
        await prepare_source(bridge, {"ok": True, "provider": "memory_companion"})
        backend = GeminiLiveBackend(VoiceAgentSettings(), Memory())
        backend.context_bridge = bridge
        calls = []

        async def tool(*args, **kwargs):
            calls.append(args)
            return {"ok": True}

        bridge.tool = tool
        await backend._memory_lock.acquire()
        await backend._handle_server_msg({"toolCall": {"functionCalls": [{
            "id": "cancelled", "name": "astrbot_remember", "args": {"content": "never write"}}]}})
        await backend._handle_server_msg({"toolCallCancellation": {"ids": ["cancelled"]}})
        backend._memory_lock.release()
        await asyncio.gather(*backend._memory_tasks, return_exceptions=True)
        assert bridge.status()["pending_requests"] == 0
        assert bridge.status()["pending_writes"] == 0
        assert calls == []
    asyncio.run(run())
