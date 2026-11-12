import asyncio
from contextlib import suppress

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


async def open_flow(tmp_path, *, shared=False, probability=100, late_seconds=.1, transport=None, keywords=(), speaker="original-speaker", provider_seconds=60):
    context = create_context_service(str(tmp_path / "history.jsonl"))
    if shared:
        await prepare_source(context.bridge, {"ok": True, "provider": "memory_companion",
            "memory_scopes": [{"umo": "group:original"}]})
    socket = Socket()
    async def connect(*args, **kwargs): return socket
    backend = create_gemini_backend(VoiceAgentSettings(gemini_api_key="synthetic"), connector=connect,
        limits=LiveLimits(late_transcription_seconds=late_seconds, provider_turn_seconds=provider_seconds))
    channel = create_channel(transport or Transport())
    session = (await channel.execute(JoinRoom("join", RoomTarget("a", "c"), 0))).session
    flow = LiveFlow(backend=backend, context=context, behavior=create_behavior(), channel=channel,
        session=session, current=lambda: True, settings=lambda: ReplySettings(probability=probability, keywords=keywords),
        on_state=lambda *args: None, on_leave=lambda **kwargs: False,
        output_state=lambda: {"volume": 50, "muted": False})
    await flow.start()
    await flow.send_input(speaker, b"\0\x40" * 320, 16000)
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
        context, socket, flow = await open_flow(tmp_path, shared=True)
        try:
            await socket.deliver(audio_message(user_text="尚未定稿"))
            await socket.close()
            async def finished():
                while flow.busy:
                    await asyncio.sleep(0)
            await asyncio.wait_for(finished(), 1)
            assert context.history.recent() == []
            assert await context.bridge.poll(wait=0) == []
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
            await flow.close()
            events.extend(await context.bridge.poll(wait=0))
            commits = {event["request_id"]: event for event in events}
            assert len(commits) == 9
            assert sorted(event["payload"]["user_text"] for event in commits.values()) == [f"问题{index}" for index in range(9)]
            assert all(event["payload"]["reply_text"] == "" for event in commits.values())
            assert all(event["scope"]["memory_scopes"] == [{"umo": "group:original"}] for event in commits.values())
            assert [row["role"] for row in context.history.recent()] == ["user"] * 9

        finally:
            blocked.set()
            await flow.close()
            await context.close()
    asyncio.run(scenario())


def test_held_live_input_waits_for_real_provider_boundary_before_late_window(tmp_path):
    async def scenario():
        context, socket, flow = await open_flow(tmp_path, shared=True, probability=0,
            keywords=("机器人",), late_seconds=.03)
        try:
            await socket.deliver({"serverContent": {"inputTranscription": {"text": "这是"}}})
            await asyncio.sleep(.1)
            assert flow.live.snapshot().state == "ready"
            assert context.history.recent() == []
            assert await context.bridge.poll(wait=0) == []
            await socket.deliver({"serverContent": {"inputTranscription": {"text": "一个完整问题"}, "turnComplete": True}})
            events = await context.bridge.poll(wait=1)
            assert len(events) == 1
            assert events[0]["payload"]["user_text"] == "这是一个完整问题"
            assert context.history.as_messages() == [{"role": "user", "content": "这是一个完整问题"}]
            assert flow.live.snapshot().state == "ready"
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary_first", [False, True])
def test_mixed_live_speakers_are_unattributed_and_cannot_authorize_remember(tmp_path, boundary_first):
    from context.models import ContextRequest
    async def scenario():
        context, socket, flow = await open_flow(tmp_path, shared=True, speaker="Alice", late_seconds=.03)
        try:
            await socket.deliver(audio_message(user_text="请记住我", text="好的", done=boundary_first))
            await flow.send_input("Bob", b"\0\x40" * 320, 16000)
            await socket.deliver({"serverContent": {"inputTranscription": {"text": "喜欢茶"}}})
            await socket.deliver({"toolCall": {"functionCalls": [{"id": "mixed-write", "name": "astrbot_remember",
                "args": {"content": "喜欢茶"}}]}})
            async def denied():
                while not any("toolResponse" in message for message in socket.sent):
                    await asyncio.sleep(0)
            await asyncio.wait_for(denied(), 1)
            response = next(message["toolResponse"]["functionResponses"][0]["response"] for message in socket.sent if "toolResponse" in message)
            assert response["ok"] is False
            proposal = flow.live.snapshot().current_proposal
            prepared = await context.prepare_context(ContextRequest(flow.session, "Alice",
                f"{flow.live.live_session_id}:{proposal.turn_id}"))
            assert not prepared.authorized_write_grants
            if not boundary_first:
                await socket.deliver({"serverContent": {"turnComplete": True}})
            async def completed():
                while flow.busy:
                    await asyncio.sleep(0)
            await asyncio.wait_for(completed(), 1)
            rows = context.history.recent()
            assert rows[0]["content"] == "请记住我喜欢茶" and rows[0]["user_key"] == ""
            events = await context.bridge.poll(wait=0)
            assert all(event["operation"] != "remember" for event in events)
            assert events[0]["payload"]["speaker"] == ""
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


def test_local_provider_deadline_without_boundary_never_commits_partial_input(tmp_path):
    async def scenario():
        context, socket, flow = await open_flow(tmp_path, shared=True, probability=0,
            keywords=("机器人",), late_seconds=.01, provider_seconds=.03)
        try:
            await socket.deliver({"serverContent": {"inputTranscription": {"text": "未结束的半句"}}})
            async def expired():
                while flow.live.snapshot().state != "failed":
                    await asyncio.sleep(0)
            await asyncio.wait_for(expired(), 1)
            await flow.close()
            assert context.history.recent() == []
            assert await context.bridge.poll(wait=0) == []
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("completed", [False, True])
def test_closing_held_live_turn_keeps_only_provider_completed_input(tmp_path, completed):
    async def scenario():
        context, socket, flow = await open_flow(tmp_path, shared=True, probability=0,
            keywords=("机器人",), late_seconds=2)
        try:
            await socket.deliver(audio_message(user_text="完整问句", text="缓存回复", done=completed))
            if completed:
                await socket.deliver({"serverContent": {"inputTranscription": {"text": "追加说明"}}})
            await flow.close()
            expected = [{"role": "user", "content": "完整问句追加说明"}] if completed else []
            assert context.history.as_messages() == expected
            events = await context.bridge.poll(wait=0)
            assert len(events) == int(completed)
            if completed:
                assert events[0]["payload"]["reply_text"] == ""
                assert events[0]["scope"]["memory_scopes"] == [{"umo": "group:original"}]
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


def test_closing_interrupted_output_does_not_infer_complete_user_input(tmp_path):
    async def scenario():
        context, socket, flow = await open_flow(tmp_path, shared=True, probability=0,
            keywords=("机器人",), late_seconds=2)
        try:
            await socket.deliver(audio_message(user_text="尚未结束", interrupted=True))
            await flow.close()
            assert context.history.as_messages() == []
            assert await context.bridge.poll(wait=0) == []
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("same_packet", [False, True])
def test_provider_interruption_waits_for_turn_complete_before_finalizing_input(tmp_path, same_packet):
    from context.models import ContextRequest

    async def scenario():
        context, socket, flow = await open_flow(tmp_path, shared=True, probability=0,
            keywords=("机器人",), late_seconds=.01)
        try:
            await socket.deliver(audio_message(user_text="请记住我喜欢茶", interrupted=True,
                                                done=same_packet))
            proposal = flow.live.snapshot().current_proposal
            if not same_packet:
                await asyncio.sleep(.04)
                assert context.history.as_messages() == []
                assert await context.bridge.poll(wait=0) == []
                prepared = await context.prepare_context(ContextRequest(flow.session,
                    "original-speaker", f"{flow.live.live_session_id}:{proposal.turn_id}"))
                assert not prepared.authorized_write_grants
                await socket.deliver({"serverContent": {"turnComplete": True}})
            events = await context.bridge.poll(wait=1)
            assert len(events) == 1
            assert events[0]["payload"]["user_text"] == "请记住我喜欢茶"
            assert events[0]["payload"]["reply_text"] == ""
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


def test_live_close_releases_playback_when_completed_input_cannot_be_saved(tmp_path, monkeypatch):
    import sqlite3

    async def scenario():
        context, socket, flow = await open_flow(tmp_path, shared=True, late_seconds=2)
        await socket.deliver(audio_message(user_text="已结束的问题", text="缓存回答", done=True))
        assert flow.busy
        connect = sqlite3.connect
        def failing_history(*args, **kwargs):
            db = connect(*args, **kwargs)
            def authorize(operation, table, *_):
                return (sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_INSERT
                        and table == "history" else sqlite3.SQLITE_OK)
            db.set_authorizer(authorize)
            return db
        try:
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3, "connect", failing_history)
                with pytest.raises((sqlite3.DatabaseError, RuntimeError)):
                    await flow.close()
            assert socket.closed
            assert not flow.busy, "保存失败仍须回收播放任务"
            # 首次保存故障仍应可查询；重试完成输入持久化。
            with suppress(RuntimeError):
                await flow.close()
            assert context.history.as_messages() == [{"role": "user", "content": "已结束的问题"}]
        finally:
            await context.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["stop", "leave", "refresh", "bind"])
def test_live_lifecycle_keeps_provider_completed_held_input(tmp_path, operation):
    from support.live_runtime import make_live_agent

    async def scenario():
        agent, _, sockets = await make_live_agent(tmp_path, reply_probability_percent=0,
            force_reply_keywords=["机器人"])
        try:
            await agent._live_flow.send_input("Alice", b"\0\x40" * 320, 16000)
            await sockets[0].deliver(audio_message(user_text="已结束的输入", text="缓存回复", done=True))
            if operation == "refresh":
                await agent.refresh(["gemini_model"])
            elif operation == "leave":
                assert (await agent.leave())["ok"]
            else:
                if operation == "bind":
                    from types import SimpleNamespace
                    async def leave(): pass
                    agent.bind_bot(SimpleNamespace(voice=SimpleNamespace(backend=None, leave=leave)))
                await agent.stop()
            assert sockets[0].closed
            assert agent.memory.as_messages() == [{"role": "user", "content": "已结束的输入"}]
        finally:
            await agent.stop()
    asyncio.run(scenario())


def test_stale_unconsumed_live_events_keep_input_in_original_source(tmp_path):
    async def scenario():
        context, socket, flow = await open_flow(tmp_path, shared=True, late_seconds=2)
        try:
            flow.current = lambda: False
            await socket.deliver(audio_message(user_text="原来源的问题", text="尚未受理的输出", done=True))
            await prepare_source(context.bridge, {"ok": True, "provider": "memory_companion",
                "memory_scopes": [{"umo": "group:new"}]})
            await flow.close()
            assert context.history.as_messages() == [{"role": "user", "content": "原来源的问题"}]
            events = await context.bridge.poll(wait=0)
            assert len(events) == 1
            assert events[0]["scope"]["memory_scopes"] == [{"umo": "group:original"}]
            assert events[0]["payload"]["reply_text"] == ""
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


def test_live_close_releases_playback_when_socket_shutdown_fails(tmp_path):
    async def scenario():
        context, socket, flow = await open_flow(tmp_path, late_seconds=2)
        await socket.deliver(audio_message(user_text="完整问题", text="缓存回答", done=True))
        assert flow.busy
        close_socket = socket.close
        async def failing_close():
            await close_socket()
            raise OSError("synthetic socket shutdown failure")
        socket.close = failing_close
        try:
            with pytest.raises((OSError, RuntimeError)):
                await flow.close()
            assert not flow.busy
            socket.close = close_socket
            with suppress(RuntimeError):
                await flow.close()
            assert context.history.as_messages() == [{"role": "user", "content": "完整问题"}]
        finally:
            socket.close = close_socket
            await context.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary_first", [False, True])
def test_new_single_speaker_recovers_personal_grant_after_mixed_turn(tmp_path, boundary_first):
    """混合轮次隔离授权，下一轮仍能按新的完整输入识别本人。"""
    async def scenario():
        context, socket, flow = await open_flow(
            tmp_path, shared=True, speaker="Alice", late_seconds=.03)
        try:
            await socket.deliver(audio_message(user_text="大家讨论", text="知道了", done=boundary_first))
            await flow.send_input("Bob", b"\0\x40" * 320, 16000)
            await socket.deliver({"serverContent": {
                "inputTranscription": {"text": "共同的话题"}, "turnComplete": True}})

            async def first_finished():
                while flow.busy or not context.history.recent():
                    await asyncio.sleep(0)
            await asyncio.wait_for(first_finished(), 1)
            await context.bridge.poll(wait=0)

            await flow.send_input("Carol", b"\0\x40" * 320, 16000)
            await socket.deliver(audio_message(user_text="请记住我喜欢咖啡"))
            await socket.deliver({"toolCall": {"functionCalls": [{
                "id": "carol-write", "name": "astrbot_remember",
                "args": {"content": "我喜欢咖啡"}}]}})

            async def answered():
                while not any("toolResponse" in message for message in socket.sent):
                    await asyncio.sleep(0)
            await asyncio.wait_for(answered(), 1)
            response = next(message["toolResponse"]["functionResponses"][0]["response"]
                for message in socket.sent if "toolResponse" in message)
            assert response.get("queued") is True, response
            events = await context.bridge.poll(wait=0)
            writes = [event for event in events if event["operation"] == "remember"]
            assert len(writes) == 1
            assert writes[0]["payload"]["speaker"] == "Carol"
            assert writes[0]["scope"]["memory_scopes"] == [{"umo": "group:original"}]
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


def test_server_interruption_waits_past_late_window_for_real_turn_complete(tmp_path):
    """服务端打断输出不等于人的输入完成，晚转录窗口从真实轮次边界开始。"""
    async def scenario():
        context, socket, flow = await open_flow(tmp_path, shared=True, probability=0,
            keywords=("机器人",), late_seconds=.02)
        try:
            await socket.deliver(audio_message(user_text="这是", text="尚未播放的回复"))
            await socket.deliver({"serverContent": {"interrupted": True}})
            # 有意越过晚转录窗口，验证interrupted不会冒充turnComplete启动定稿。
            await asyncio.sleep(.08)
            assert context.history.recent() == []
            assert await context.bridge.poll(wait=0) == []
            await socket.deliver({"serverContent": {
                "inputTranscription": {"text": "同一个完整问题"}, "turnComplete": True}})
            events = await context.bridge.poll(wait=1)
            assert len(events) == 1
            assert events[0]["payload"]["user_text"] == "这是同一个完整问题"
            assert events[0]["payload"]["reply_text"] == ""
            assert context.history.as_messages() == [
                {"role": "user", "content": "这是同一个完整问题"}]
        finally:
            await flow.close()
            await context.close()
    asyncio.run(scenario())


async def closing_agent(tmp_path, operation, **settings):
    import json

    from support.live_runtime import make_live_agent

    started = asyncio.Event()
    stopping = []
    class CloseOnCompletion(Socket):
        async def __anext__(self):
            message = await super().__anext__()
            if json.loads(message).get("serverContent", {}).get("turnComplete"):
                stopping.append(asyncio.create_task(agent.stop_current_reply()
                    if operation == "stop" else agent.leave()))
                started.set()
            return message
    agent, playback, sockets = await make_live_agent(tmp_path, socket_factory=CloseOnCompletion,
                                                    **settings)
    await agent._live_flow.send_input("speaker", b"\0\x40" * 320, 16000)
    return agent, playback, sockets, started, stopping


@pytest.mark.parametrize("operation", ["stop", "leave"])
def test_stop_before_live_confirmation_starts_preserves_completed_input(tmp_path, operation):
    async def scenario():
        agent, playback, sockets, started, stopping = await closing_agent(tmp_path, operation)
        async def unconfirmed(timeout=30):
            raise AssertionError("关闭必须先于播放确认开始")
        playback.wait_tts_complete = unconfirmed
        try:
            await sockets[0].queue.put((audio_message(user_text="完整用户问题", text="未确认回复", done=True), None))
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(stopping[0], 1)
            assert agent.memory.as_messages() == [{"role": "user", "content": "完整用户问题"}]
            assert agent.memory.recent()[0]["user_key"] == "speaker"
            assert sockets[0].closed
        finally:
            await agent.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["stop", "leave"])
def test_retired_live_input_write_failure_does_not_prevent_resource_stop(tmp_path, monkeypatch, operation):
    import sqlite3

    from support.live_runtime import make_live_agent

    async def scenario():
        agent, playback, sockets = await make_live_agent(tmp_path, reply_probability_percent=0,
            force_reply_keywords=["机器人"])
        await sockets[0].deliver(audio_message(user_text="已结束用户问题", text="缓存草稿", done=True))
        connect = sqlite3.connect
        def failing_history(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_authorizer(lambda operation, table, *_: sqlite3.SQLITE_DENY
                if operation == sqlite3.SQLITE_INSERT and table == "history" else sqlite3.SQLITE_OK)
            return db
        try:
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3, "connect", failing_history)
                if operation == "stop":
                    with pytest.raises(RuntimeError):
                        await agent.stop_current_reply()
                else:
                    assert (await agent.leave())["ok"] is False
            assert sockets[0].closed
            assert playback.stops > 0
            if operation == "leave":
                assert not agent.channels.snapshot().confirmed
        finally:
            await agent.stop()
    asyncio.run(scenario())


def test_cancelled_leave_waits_for_accepted_input_write_and_preserves_cancellation(tmp_path, monkeypatch):
    import threading

    async def scenario():
        agent, playback, sockets, started, stopping = await closing_agent(tmp_path, "leave",
            reply_probability_percent=0, force_reply_keywords=["机器人"])
        entered, release = threading.Event(), threading.Event()
        finish = agent.context_service.finish_input
        def blocked_write(input_ref):
            entered.set()
            assert release.wait(3)
            return finish(input_ref)
        monkeypatch.setattr(agent.context_service, "finish_input", blocked_write)
        try:
            await sockets[0].queue.put((audio_message(user_text="原房间已结束问题", done=True), None))
            assert await asyncio.to_thread(entered.wait, 1)
            await asyncio.wait_for(started.wait(), 1)
            stopping[0].cancel()
            await asyncio.sleep(0)
            stopping[0].cancel()
            await asyncio.sleep(0)
            assert not stopping[0].done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await stopping[0]
            assert agent.memory.as_messages() == [{"role": "user", "content": "原房间已结束问题"}]
            assert sockets[0].closed and playback.stops > 0
            assert not agent.channels.snapshot().confirmed
        finally:
            release.set()
            await asyncio.gather(*stopping, return_exceptions=True)
            await agent.stop()
    asyncio.run(scenario())
