import asyncio

import pytest
from support.live_runtime import Socket, audio_message

from app.live import LiveFlow
from behavior import create_behavior
from behavior.models import ReplySettings
from channels import create_channel
from channels.models import AudioPushResult, JoinRoom, RoomTarget
from context import create_context_service
from dialogue import create_gemini_backend
from dialogue.models import LiveLimits
from operations.models import VoiceAgentSettings
from tests.support.context_fixtures import prepare_source


class Transport:
    async def join(self, target): return "rtc"
    async def leave(self): pass
    async def retire_output(self): pass
    async def push_audio(self, pcm, rate, *, finish=False): return AudioPushResult(True)
    async def wait_playback(self, timeout): return AudioPushResult(True)


async def open_flow(tmp_path, *, shared=False, probability=100, late_seconds=.1, transport=None):
    context = create_context_service(str(tmp_path / "history.jsonl"))
    if shared:
        await prepare_source(context.bridge, {"ok": True, "provider": "memory_companion",
            "memory_scopes": [{"umo": "group:original"}]})
    socket = Socket()
    async def connect(*args, **kwargs): return socket
    backend = create_gemini_backend(VoiceAgentSettings(gemini_api_key="synthetic"), connector=connect,
        limits=LiveLimits(late_transcription_seconds=late_seconds))
    channel = create_channel(transport or Transport())
    session = (await channel.execute(JoinRoom("join", RoomTarget("a", "c"), 0))).session
    flow = LiveFlow(backend=backend, context=context, behavior=create_behavior(), channel=channel,
        session=session, current=lambda: True, settings=lambda: ReplySettings(probability=probability),
        on_state=lambda *args: None, on_leave=lambda **kwargs: False,
        output_state=lambda: {"volume": 50, "muted": False})
    await flow.start()
    await flow.send_input("original-speaker", b"\0\x40" * 320, 16000)
    return context, socket, flow


def test_allowed_turn_commits_late_transcription_as_one_complete_input(tmp_path):
    async def scenario():
        context, socket, flow = await open_flow(tmp_path)
        try:
            await socket.deliver(audio_message(user_text="请记住", text="好的", done=True))
            await socket.deliver({"serverContent": {"inputTranscription": {"text": "我喜欢茶"}}})
            async def committed():
                while flow.busy or not context.history.recent():
                    await asyncio.sleep(0)
            await asyncio.wait_for(committed(), 1)
            assert [row["content"] for row in context.history.recent()] == ["请记住我喜欢茶", "好的"]
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


def test_live_remember_waits_for_context_grant_without_waiting_for_tool_dependent_turn_complete(tmp_path):
    async def scenario():
        context, socket, flow = await open_flow(tmp_path, shared=True)
        try:
            await socket.deliver(audio_message(user_text="请记住我喜欢茶"))
            await socket.deliver({"toolCall": {"functionCalls": [{"id": "remember-original",
                "name": "astrbot_remember", "args": {"content": "我喜欢茶"}}]}})
            events = await context.bridge.poll(wait=1)
            assert len(events) == 1
            event = events[0]
            assert event["operation"] == "remember"
            assert event["payload"] == {"content": "我喜欢茶", "speaker": "original-speaker"}
            async def replied():
                while not any("toolResponse" in message for message in socket.sent):
                    await asyncio.sleep(0)
            await asyncio.wait_for(replied(), 1)
            response = next(message["toolResponse"]["functionResponses"][0]["response"]
                            for message in socket.sent if "toolResponse" in message)
            assert response["queued"] is True
            await socket.deliver({"serverContent": {"turnComplete": True}})
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


def test_provider_failure_before_input_finalization_does_not_hold_playback(tmp_path):
    async def scenario():
        context, socket, flow = await open_flow(tmp_path)
        try:
            await socket.deliver(audio_message(user_text="尚未定稿"))
            await socket.close()
            async def finished():
                while flow.busy:
                    await asyncio.sleep(0)
            await asyncio.wait_for(finished(), 1)
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


def test_muted_live_reply_never_enters_confirmed_assistant_history(tmp_path):
    from support.live_runtime import make_live_agent
    async def scenario():
        agent, playback, sockets = await make_live_agent(tmp_path)
        try:
            assert (await agent.set_output_controls(muted=True))["ok"]
            await sockets[0].deliver(audio_message(user_text="我的问题", text="被静音的回答", done=True))
            async def finished():
                while agent._live_flow.busy:
                    await asyncio.sleep(0)
            await asyncio.wait_for(finished(), 1)
            assert all(row["role"] != "assistant" for row in agent.memory.recent())
            assert playback.chunks == []
        finally:
            await agent.stop()
    asyncio.run(scenario())


def test_rejected_expression_still_waits_for_the_complete_user_input(tmp_path):
    async def scenario():
        context, socket, flow = await open_flow(tmp_path, probability=0)
        try:
            await socket.deliver(audio_message(user_text="这是"))
            await socket.deliver({"serverContent": {
                "inputTranscription": {"text": "完整的问题"}, "turnComplete": True}})
            async def committed():
                while not context.history.recent():
                    await asyncio.sleep(0)
            await asyncio.wait_for(committed(), 1)
            assert [row["content"] for row in context.history.recent()] == ["这是完整的问题"]
            assert not flow.busy
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["memory_path", "memory_max_turns"])
def test_joined_live_storage_refresh_restores_the_new_context_before_return(tmp_path, field):
    from support.live_runtime import make_live_agent
    async def scenario():
        agent, _, sockets = await make_live_agent(tmp_path)
        try:
            if field == "memory_path":
                agent.settings.memory_path = str(tmp_path / "new-history.jsonl")
            else:
                agent.settings.memory_max_turns += 1
            await agent.refresh([field])
            assert agent._live_flow is not None
            assert agent.backend.session_active
            assert sockets[0].closed and len(sockets) == 2
            await agent._live_flow.send_input("speaker", b"\0\x40" * 320, 16000)
            await sockets[-1].deliver(audio_message(user_text="新的问题", text="新的回答", done=True))
            async def committed():
                while agent._live_flow.busy or not agent.memory.recent():
                    await asyncio.sleep(0)
            await asyncio.wait_for(committed(), 1)
            assert [row["content"] for row in agent.memory.recent()] == ["新的问题", "新的回答"]
        finally:
            await agent.stop()
    asyncio.run(scenario())


def test_live_probability_rejection_collects_only_final_complete_input(tmp_path):
    async def scenario():
        context, socket, flow = await open_flow(tmp_path, shared=True, probability=0)
        try:
            await socket.deliver(audio_message(user_text="这是"))
            assert await context.bridge.poll(wait=0) == []
            await socket.deliver({"serverContent": {"inputTranscription": {"text": "完整的问题"}, "turnComplete": True}})
            events = await context.bridge.poll(wait=1)
            assert len(events) == 1
            assert events[0]["payload"]["user_text"] == "这是完整的问题"
            assert events[0]["payload"]["reply_text"] == ""
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


def test_ninth_live_turn_with_busy_output_still_collects_original_input(tmp_path):
    async def scenario():
        blocked = asyncio.Event()
        class PendingDrain(Transport):
            async def wait_playback(self, timeout):
                await blocked.wait()
                return AudioPushResult(True)
        context, socket, flow = await open_flow(tmp_path, shared=True,
                                               late_seconds=1, transport=PendingDrain())
        try:
            for index in range(9):
                await socket.deliver(audio_message(user_text=f"问题{index}", text=f"回答{index}", done=True))
            assert 1 <= len(flow._outputs) <= 8
            events = []
            async def collected():
                while True:
                    events.extend(await context.bridge.poll(wait=0))
                    if any(event["payload"].get("user_text") == "问题8" for event in events):
                        return
                    await asyncio.sleep(0)
            await asyncio.wait_for(collected(), 2)
            ninth = [event for event in events if event["payload"].get("user_text") == "问题8"]
            assert len(ninth) == 1
            assert ninth[0]["payload"]["reply_text"] == ""
            assert ninth[0]["scope"]["memory_scopes"] == [{"umo": "group:original"}]
        finally:
            blocked.set()
            await flow.close()
            await context.close()
    asyncio.run(scenario())
