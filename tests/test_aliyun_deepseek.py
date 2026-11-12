"""Local protocol servers exercise streaming, cancellation and secret loading."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from support.channel_fixtures import establish_room
from support.dialogue_fixtures import aliyun_backend as AliyunDeepSeekBackend
from test_voice_operations import make_agent

from channels.models import OutputSourceError
from dialogue.models import VoiceStreamInterrupted
from operations import load_voice_agent_settings
from operations.models import VoiceAgentSettings


async def run_expression_protocol(backend, pcm, *, sample_rate, user_key, channel_key, on_audio):
    """按公开识别/分类/表达契约驱动真实供应商，不调用旧复合后端入口。"""
    import time
    from uuid import uuid4

    from behavior.models import AttemptPermit, SpeechIntent
    from channels.models import RoomTarget, SessionRef
    from context.models import ContextRef, ContextRevision, PreparedContext
    from dialogue import audio_output
    from dialogue.models import AudioInput, ControlInput

    session = SessionRef("test-run", "test-binding", uuid4().hex, RoomTarget("area", "channel"))
    input_ref = uuid4().hex
    permit = AttemptPermit(session, 1, input_ref=input_ref, expires_at=time.monotonic()+5)
    input = AudioInput(input_ref, session, pcm, sample_rate, user_key, permit.attempt_id)
    recognized = await backend.recognize(input, permit)
    if recognized.status != "recognized":
        return SimpleNamespace(user_text="", text="", pcm16=b"", user_key=user_key, control=None)
    result = await backend.interpret_control(ControlInput(recognized.text, input.input_ref, session, user_key))
    if result.status != "candidate":
        raise ValueError(result.reason)
    if result.candidate.leave or result.candidate.volume is not None:
        return SimpleNamespace(user_text=recognized.text, text="", pcm16=b"", user_key=user_key,
                               control=result.candidate)
    context = PreparedContext(ContextRef(session, 0, "local"), ContextRevision(0), backend.settings.persona, ())
    action = uuid4().hex
    intent = SpeechIntent(action, action, session, input.input_ref, context.version,
                          time.monotonic()+5, recognized.text, user_key)
    audio = audio_output(backend.generate(intent, context))
    output = bytearray()
    try:
        async for frame in audio.frames():
            await on_audio(frame.pcm16, frame.sample_rate)
            output.extend(frame.pcm16)
        return SimpleNamespace(user_text=recognized.text, text=audio.evidence().text,
                               pcm16=bytes(output), user_key=user_key, control=None)
    finally:
        await audio.aclose()


def test_local_keys_do_not_expand_shell_or_override_process_env(tmp_path, monkeypatch):
    path = tmp_path / '.env.local'
    path.write_text('DASHSCOPE_API_KEY="local-${UNSET}"\nDEEPSEEK_API_KEY=local-deepseek\n')
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'process-deepseek')
    settings, _ = load_voice_agent_settings(local_environment_path=path)
    assert settings.dashscope_api_key == 'local-${UNSET}'
    assert settings.deepseek_api_key == 'process-deepseek'
    assert 'local-${UNSET}' not in repr(settings)
    assert 'process-deepseek' not in repr(settings)
    assert 'DASHSCOPE_API_KEY' not in os.environ


@asynccontextmanager
async def model_servers(*, fail_asr=False, truncate=False, control=False):
    state = SimpleNamespace(
        text=[], chat_requests=[], audio_seen=asyncio.Event(), llm_finished=False,
        open_sockets=0, asr_bytes=bytearray(), sockets_idle=asyncio.Event(),
    )

    async def websocket(request):
        assert request.headers['Authorization'] == 'Bearer fake-dashscope'
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state.open_sockets += 1
        state.sockets_idle.clear()
        try:
            run = await ws.receive_json()
            task = run['header']['task_id']
            kind = run['payload']['task']

            async def event(name, payload=None, **header):
                await ws.send_json({'header': {'event': name, 'task_id': task, **header},
                                    'payload': payload or {}})

            if kind == 'asr' and fail_asr:
                await event('task-failed', error_code='InvalidParameter')
                return ws
            await event('task-started')
            async for message in ws:
                if message.type == web.WSMsgType.BINARY:
                    state.asr_bytes.extend(message.data)
                    continue
                if message.type != web.WSMsgType.TEXT:
                    break
                data = json.loads(message.data)
                if data['header']['action'] == 'continue-task':
                    text = data['payload']['input']['text']
                    state.text.append(text)
                    # PCM sample split across two WebSocket binary messages.
                    await ws.send_bytes(b'\x01\x00\x02')
                    await ws.send_bytes(b'\x00\x03\x00')
                if data['header']['action'] == 'finish-task':
                    if kind == 'asr':
                        await event('result-generated', {'output': {'sentence': {
                            'sentence_id': 1, 'text': '错误的中间结果', 'sentence_end': False}}})
                        await event('result-generated', {'output': {'sentence': {
                            'sentence_id': 1, 'text': '你好', 'sentence_end': True}}})
                        await event('result-generated', {'output': {'sentence': {
                            'sentence_id': 0, 'text': 'heartbeat', 'sentence_end': True,
                            'heartbeat': True}}})
                    await event('task-finished')
                    break
        finally:
            state.open_sockets -= 1
            if not state.open_sockets:
                state.sockets_idle.set()
        return ws

    async def chat(request):
        assert request.headers['Authorization'] == 'Bearer fake-deepseek'
        body = await request.json()
        state.chat_requests.append(body)
        assert body['model'] == 'deepseek-flash'
        assert body['thinking'] == {'type': 'disabled'}
        if body.get('response_format'):
            value = {'leave': control, 'volume': None}
            return web.json_response({'choices': [{'message': {'content': json.dumps(value)}}]})
        if body.get("tools"):
            assert body["stream"] is False
            return web.json_response({"choices": [{"message": {"content": "共享记忆回复。"}}]})
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)

        async def delta(content, reason=None):
            event = {'choices': [{'delta': {'content': content}, 'finish_reason': reason}]}
            record = ('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()
            # Split JSON and UTF-8 across writes, as happens on real networks.
            for cut in (record[:23], record[23:25], record[25:]):
                await response.write(cut)

        await delta('**你好**。')
        # The LLM cannot finish until the client has played its first PCM.
        await asyncio.wait_for(state.audio_seen.wait(), 3)
        if not truncate:
            await delta('再见。')
            await delta('', 'stop')
            await response.write(b'data: [DONE]\n\n')
        state.llm_finished = True
        return response

    app = web.Application()
    app.router.add_get('/speech', websocket)
    app.router.add_post('/chat/completions', chat)
    runner = web.AppRunner(app)
    await runner.setup()
    server = web.TCPSite(runner, '127.0.0.1', 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    settings = VoiceAgentSettings(
        backend='aliyun_deepseek', dashscope_api_key='fake-dashscope',
        deepseek_api_key='fake-deepseek', dashscope_ws_url=f'ws://127.0.0.1:{port}/speech',
        deepseek_base_url=f'http://127.0.0.1:{port}', reply_probability_percent=100,
    )
    backend = AliyunDeepSeekBackend(settings)
    try:
        yield backend, state
    finally:
        await backend.aclose()
        await runner.cleanup()


def test_audio_reaches_sink_before_llm_finishes_and_controls_are_not_spoken():
    async def run():
        async with model_servers() as (backend, state):
            output = bytearray()

            async def sink(pcm, rate):
                assert rate == 24000 and len(pcm) % 2 == 0
                if not output:
                    assert not state.llm_finished
                output.extend(pcm)
                state.audio_seen.set()

            reply = await run_expression_protocol(backend,
                b'\x01\x00' * 24000, sample_rate=24000,
                user_key='oopz:member', channel_key='area/channel', on_audio=sink,
            )
            assert reply.user_text == '你好'
            assert reply.text == '你好。再见。'
            assert state.text == ['你好。', '再见。']
            assert reply.pcm16 == bytes(output)
            assert len(state.asr_bytes) == 32000  # Resampled from 24 kHz to 16 kHz.
            assert len(state.chat_requests) == 2  # Control classification then speech.
            await asyncio.wait_for(state.sockets_idle.wait(), 1)
            assert state.open_sockets == 0

    asyncio.run(run())


def test_control_request_never_opens_tts():
    async def run():
        async with model_servers(control=True) as (backend, state):
            sink = AsyncMock()
            reply = await run_expression_protocol(backend,
                b'\x01\x00' * 16000, sample_rate=16000,
                user_key='member', channel_key='channel', on_audio=sink,
            )
            assert reply.control is not None and reply.control.leave
            assert len(state.chat_requests) == 1
            assert not state.text and not sink.called

    asyncio.run(run())


@pytest.mark.parametrize('interruption', ['barge_in', 'cancel', 'truncated_sse'])
def test_stream_failure_cleans_up_without_retrying_audible_reply(interruption):
    async def run():
        async with model_servers(truncate=interruption == 'truncated_sse') as (backend, state):
            async def sink(pcm, rate):
                state.audio_seen.set()
                if interruption == 'barge_in':
                    raise VoiceStreamInterrupted()
                if interruption == 'cancel':
                    raise asyncio.CancelledError()

            expected = {'barge_in': VoiceStreamInterrupted, 'cancel': asyncio.CancelledError,
                        'truncated_sse': OutputSourceError}[interruption]
            with pytest.raises(expected):
                await run_expression_protocol(backend,
                    b'\x01\x00' * 16000, sample_rate=16000,
                    user_key='member', channel_key='channel', on_audio=sink,
                )
            assert len(state.chat_requests) == 2
            await asyncio.wait_for(state.sockets_idle.wait(), 1)
            assert state.open_sockets == 0

    asyncio.run(run())


def test_asr_task_failure_does_not_reach_llm():
    async def run():
        async with model_servers(fail_asr=True) as (backend, state):
            with pytest.raises(RuntimeError, match='InvalidParameter'):
                await backend.asr(b'\x00\x00', 16000)
            assert not state.chat_requests

    asyncio.run(run())


@pytest.mark.parametrize('invalidate', [False, True])
def test_room_stream_is_not_replayed_and_stale_audio_is_rejected(tmp_path, invalidate):
    from uuid import uuid4

    from channels.models import LeaveRoom
    from dialogue import create_expression_job
    from dialogue.models import Recognition

    async def run():
        agent = make_agent(tmp_path)
        await establish_room(agent)
        agent.settings.min_utterance_ms = 0
        agent.settings.reply_probability_percent = 100
        agent.settings.voice_leave_enabled = False
        agent.settings.voice_volume_enabled = False
        pushed = []
        first = asyncio.Event()
        async def push(pcm, rate, **kwargs):
            pushed.append((pcm, kwargs.get('finish', False)))
            if pcm:
                first.set()
            return {'ok': True}
        agent.duplex.push_tts_pcm = push
        agent.duplex.stop_tts = AsyncMock(return_value={'ok': True})
        class Backend:
            async def recognize(self, input, permit):
                return Recognition(input.input_ref, input.session, permit.attempt_id,
                                   input.user_key, 'recognized', '你好')
            def generate(self, intent, context, **kwargs):
                async def produce(emit):
                    await emit('text_final', text='收到')
                    await emit('audio', pcm16=b'\x01\x00', sample_rate=24000)
                    if invalidate:
                        await asyncio.Event().wait()
                return create_expression_job(intent.action_id, intent.session, produce,
                                             deadline=intent.deadline)
        agent.backend = Backend()
        worker = asyncio.create_task(agent._handle_utterance('member', b'\x00\x00', 16000))
        await asyncio.wait_for(first.wait(), 1)
        if invalidate:
            await agent.channels.execute(LeaveRoom(uuid4().hex, agent.channels.snapshot().session))
        await asyncio.wait_for(worker, 1)
        assert [pcm for pcm, finish in pushed if not finish] == [b'\x01\x00']
        assert any(finish for _, finish in pushed) is not invalidate
        assert agent.turns == 1
        if not invalidate:
            assert not agent._speaking
    asyncio.run(run())


def test_barge_in_cancels_generation_without_cancelling_room_worker(tmp_path):
    from dialogue import create_expression_job
    from dialogue.models import Recognition

    async def run():
        agent = make_agent(tmp_path)
        await establish_room(agent)
        agent.settings.min_utterance_ms = 0
        agent.settings.reply_probability_percent = 100
        agent.settings.voice_leave_enabled = False
        agent.settings.voice_volume_enabled = False
        agent.duplex.push_tts_pcm = AsyncMock(return_value={'ok': True})
        started = asyncio.Event()
        cancelled = asyncio.Event()
        class Backend:
            async def recognize(self, input, permit):
                return Recognition(input.input_ref, input.session, permit.attempt_id,
                                   input.user_key, 'recognized', '你好')
            def generate(self, intent, context, **kwargs):
                async def produce(emit):
                    await emit('audio', pcm16=b'\x01\x00', sample_rate=24000)
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                return create_expression_job(intent.action_id, intent.session, produce,
                                             deadline=intent.deadline)
        agent.backend = Backend()
        worker = asyncio.create_task(agent._handle_utterance('member', b'\x00\x00', 16000))
        await asyncio.wait_for(started.wait(), 1)
        await agent._interrupt_speaking()
        await asyncio.wait_for(worker, .5)
        assert cancelled.is_set()
        assert not worker.cancelled()
        assert agent._stream_reply_task is None
        assert not agent._speaking
    asyncio.run(run())


def test_streaming_barge_in_ignores_silence_and_requires_hold_duration(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.barge_in_hold_ms = 300
        agent._speaking = True
        agent.duplex.stop_tts = AsyncMock(return_value={'ok': True})

        class Backend:
            def capabilities(self):
                from dialogue.models import DialogueCapabilities
                return DialogueCapabilities(streaming_utterance=True)

        agent.backend = Backend()
        await agent._on_remote_pcm('member', b'\x00\x00' * 1600, 16000)
        assert agent._speaking
        for _ in range(2):
            await agent._on_remote_pcm('member', b'\x00\x20' * 1600, 16000)
            assert agent._speaking
        await agent._on_remote_pcm('member', b'\x00\x20' * 1600, 16000)
        assert not agent._speaking
        agent.duplex.stop_tts.assert_awaited_once()

    asyncio.run(run())


def test_concurrent_generation_keeps_each_output_sink_separate():
    async def run():
        async with model_servers() as (backend, state):
            outputs = [bytearray(), bytearray()]

            async def generate(index):
                async def sink(pcm, rate):
                    assert rate == 24000
                    outputs[index].extend(pcm)
                    state.audio_seen.set()

                return await run_expression_protocol(backend,
                    b'\x01\x00' * 16000, sample_rate=16000,
                    user_key=f'member-{index}', channel_key='channel', on_audio=sink,
                )

            replies = await asyncio.wait_for(asyncio.gather(generate(0), generate(1)), 5)
            assert all(outputs), "each request must receive its own audio"
            assert [reply.pcm16 for reply in replies] == list(map(bytes, outputs))
            assert [reply.user_key for reply in replies] == ['member-0', 'member-1']

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["_interrupt_speaking", "stop_current_reply", "manual_stop"])
def test_interrupt_retires_inflight_playback_and_room_remains_usable(tmp_path, operation):
    async def run():
        async with model_servers() as (backend, state):
            agent = make_agent(tmp_path)
            await establish_room(agent)
            agent.backend = backend
            started, released = asyncio.Event(), asyncio.Event()
            events = []
            async def push(pcm, rate, **kwargs):
                started.set()
                try:
                    await released.wait()
                except asyncio.CancelledError:
                    await released.wait()
                events.append("push")
                return {"ok": True}
            async def stop():
                events.append("stop")
                return {"ok": True}
            agent.duplex.push_tts_pcm, agent.duplex.stop_tts = push, stop
            worker = asyncio.create_task(agent.speak_text("manual") if operation == "manual_stop"
                else agent._handle_utterance("member", b"\x01\x00" * 16000, 16000))
            await asyncio.wait_for(started.wait(), 3)
            interrupt = asyncio.create_task(getattr(agent,
                "stop_current_reply" if operation == "manual_stop" else operation)())
            await asyncio.sleep(0)
            released.set()
            await asyncio.wait_for(interrupt, 3)
            state.audio_seen.set()
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 3)
            assert events[0] == "push" and "stop" in events[1:]
            assert agent.channels.snapshot().confirmed
            assert (await agent.speak_text("next manual"))["ok"]
            await agent.stop()
    asyncio.run(run())


def test_manual_speech_uses_own_text_and_never_enters_automatic_audio_sink(tmp_path):
    async def run():
        async with model_servers() as (backend, state):
            agent = make_agent(tmp_path)
            await establish_room(agent)
            agent.backend = backend
            agent.duplex.push_tts_pcm = AsyncMock(return_value={"ok": True})
            first_audio, release = asyncio.Event(), asyncio.Event()
            automatic_audio = []
            async def sink(pcm, rate):
                automatic_audio.append(pcm)
                first_audio.set()
                state.audio_seen.set()
                await release.wait()
            automatic = asyncio.create_task(run_expression_protocol(backend,
                b"\x01\x00" * 16000, sample_rate=16000,
                user_key="member", channel_key="area/channel", on_audio=sink))
            await first_audio.wait()
            manual = asyncio.create_task(agent.speak_text("手动播报"))
            await asyncio.sleep(0)
            release.set()
            reply, result = await asyncio.wait_for(asyncio.gather(automatic, manual), 5)
            assert result["ok"] and "手动播报" in state.text
            assert len(automatic_audio) == 4 and len(reply.pcm16) == 12
            assert result["pcm_bytes"] == 6
            await agent.stop()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["barge", "stop"])
def test_interrupted_stream_keeps_recognized_input_under_original_identity(tmp_path, operation):
    async def run():
        async with model_servers() as (backend, state):
            agent = make_agent(tmp_path)
            await agent.join("original-area", "original-room")
            agent.backend = backend
            audible = asyncio.Event()
            async def push(*args, **kwargs):
                audible.set()
                return {"ok": True}
            agent.duplex.push_tts_pcm = push
            worker = asyncio.create_task(agent._handle_utterance("original-member", b"\x01\x00" * 16000, 16000))
            await asyncio.wait_for(audible.wait(), 3)
            assert not state.llm_finished
            await (agent._interrupt_speaking() if operation == "barge" else agent.stop_current_reply())
            await asyncio.gather(worker, return_exceptions=True)
            state.audio_seen.set()
            rows = agent.memory.recent(user_key="oopz:original-member")
            assert [(row["role"], row["content"]) for row in rows] == [("user", "你好")]
            assert rows[0]["channel_key"] == "original-area/original-room"
            await agent.stop()
    asyncio.run(run())
