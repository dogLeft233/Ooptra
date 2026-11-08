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
async def model_servers(*, fail_asr=False, truncate=False, control=False, invalid_answer="", incomplete_asr=False):
    state = SimpleNamespace(
        text=[], chat_requests=[], audio_seen=asyncio.Event(), llm_finished=False,
        open_sockets=0, asr_bytes=bytearray(), sockets_idle=asyncio.Event(),
        first_input=asyncio.Event(), asr_parameters=None,
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

            if kind == 'asr':
                state.asr_parameters = run['payload']['parameters']
            if kind == 'asr' and fail_asr:
                await event('task-failed', error_code='InvalidParameter')
                return ws
            await event('task-started')
            async for message in ws:
                if message.type == web.WSMsgType.BINARY:
                    state.asr_bytes.extend(message.data)
                    await event('result-generated', {'output': {'sentence': {
                        'sentence_id': 1, 'text': '临时识别', 'sentence_end': False}}})
                    state.first_input.set()
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
                            'sentence_id': 1, 'text': '你好', 'sentence_end': not incomplete_asr}}})
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
        assert body['thinking'] == {'type': 'enabled'}
        assert body['reasoning_effort'] == 'low'
        if body.get('response_format'):
            value = {'leave': control, 'volume': None}
            return web.json_response({'choices': [{'message': {'content': json.dumps(value)}}]})
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)

        async def delta(content, reason=None):
            event = {'choices': [{'delta': {'content': content}, 'finish_reason': reason}]}
            record = ('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()
            # Split JSON and UTF-8 across writes, as happens on real networks.
            for cut in (record[:23], record[23:25], record[25:]):
                await response.write(cut)

        await response.write(('data: '+json.dumps({'choices':[{'delta':{'reasoning_content':'秘密推理，不得播报。'},'finish_reason':None}]},ensure_ascii=False)+'\n\n').encode())
        if invalid_answer:
            if invalid_answer == 'markup':
                await delta('<thi')
                await delta('nk>秘密思考。</think>')
            await delta('', 'stop')
            await response.write(b'data: [DONE]\n\n')
            return response
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


def test_streaming_recognition_uploads_before_end_and_only_final_text_is_authoritative():
    async def run():
        import time

        from behavior.models import AttemptPermit
        from channels.models import RoomTarget, SessionRef
        from dialogue.models import AudioInput
        async with model_servers() as (backend, state):
            session = SessionRef("process", "binding", "session", RoomTarget("a", "c"))
            permit = AttemptPermit(session, 1, "input", time.monotonic()+5)
            stream = backend.open_recognition(AudioInput("input", session, b"", 16000,
                                                         "peer", permit.attempt_id), permit)
            try:
                stream.feed(b"\x00\x20" * 1600)
                await asyncio.wait_for(state.first_input.wait(), 1)
                assert state.asr_parameters['intermediate_result_enabled'] is True
                # Wait for the real receiver to expose the revision, without ending the audio.
                async def partial():
                    while not stream.partial():
                        await asyncio.sleep(0)
                await asyncio.wait_for(partial(), 1)
                assert stream.partial()[0].text == "临时识别"
                assert not stream.partial()[0].final
                stream.feed(b"\x00\x20" * 1600)
                stream.finish_input()
                result = await stream.result()
                assert result.status == "recognized" and result.text == "你好"
                assert len(state.asr_bytes) == 6400
                assert not state.chat_requests and not state.text
            finally:
                await stream.aclose()
            await asyncio.wait_for(state.sockets_idle.wait(), 1)
    asyncio.run(run())


@pytest.mark.parametrize('end', ['reply', 'leave', 'gap'])
def test_room_capture_streams_before_cut_and_releases_original_attempt(tmp_path, end):
    async def run():
        async with model_servers() as (backend, state):
            agent = make_agent(tmp_path)
            await agent.backend.aclose()
            agent.backend = backend
            agent.settings.min_utterance_ms = 60
            agent.settings.silence_ms = 60
            agent.settings.cascade_turn_limit = 1
            await agent.join('a', 'c')
            pushed = asyncio.Event()
            async def push(pcm, rate, **kwargs):
                if pcm:
                    state.audio_seen.set()
                    pushed.set()
                return {'ok': True}
            agent.duplex.push_tts_pcm = push
            try:
                await agent._on_remote_pcm('member', b'\x00\x20' * 960, 16000)
                await asyncio.wait_for(state.first_input.wait(), 1)
                assert agent.status()['cascade_attempts'] == 1
                assert not state.chat_requests
                if end == 'leave':
                    await agent.leave()
                elif end == 'gap':
                    # Invalid PCM resets both the channel window and in-flight recognition.
                    await agent._on_remote_pcm('member', b'odd', 16000)
                    await agent.leave()
                else:
                    # Even though the limit is now exhausted, the admitted utterance may finish.
                    await agent._on_remote_pcm('member', bytes(1920), 16000)
                    await asyncio.wait_for(pushed.wait(), 1)
                    async def completed():
                        while agent.last_reply != '你好。再见。':
                            await asyncio.sleep(0)
                    await asyncio.wait_for(completed(), 1)
                    assert agent.memory.as_messages(user_key='oopz:member') == [
                        {'role':'user', 'content':'你好'},
                        {'role':'assistant', 'content':'你好。再见。'}]
                    assert state.text == ['你好。', '再见。']
                    assert agent.status()['cascade_attempts'] == 1
                    await agent.leave()
                await asyncio.wait_for(state.sockets_idle.wait(), 1)
                assert state.open_sockets == 0
                if end != 'reply':
                    assert not state.chat_requests and not agent.memory.as_messages()
            finally:
                await agent.close()
    asyncio.run(run())


@pytest.mark.parametrize('invalid_answer', ['markup', 'reasoning_only'])
def test_reasoning_without_a_valid_formal_answer_produces_no_speech(invalid_answer):
    async def run():
        async with model_servers(invalid_answer=invalid_answer) as (backend, state):
            with pytest.raises(OutputSourceError):
                await run_expression_protocol(backend, b"\x00\x20" * 1600, sample_rate=16000,
                    user_key='peer', channel_key='a/c', on_audio=AsyncMock())
            assert state.text == []
    asyncio.run(run())


@pytest.mark.parametrize('shutdown', ['owner', 'backend', 'overflow'])
def test_streaming_recognition_shutdown_closes_active_socket(shutdown):
    async def run():
        import time

        from behavior.models import AttemptPermit
        from channels.models import RoomTarget, SessionRef
        from dialogue.models import AudioInput

        async with model_servers() as (backend, state):
            session = SessionRef('p', 'b', 's', RoomTarget('a', 'c'))
            permit = AttemptPermit(session, 1, 'input', time.monotonic()+5)
            stream = backend.open_recognition(AudioInput('input', session, b'', 16000,
                                                         'peer', permit.attempt_id), permit)
            stream.feed(b'\x00\x20' * 1600)
            await asyncio.wait_for(state.first_input.wait(), 1)
            if shutdown == 'backend':
                await backend.aclose()
            else:
                if shutdown == 'overflow':
                    with pytest.raises(ValueError, match='溢出'):
                        for _ in range(65):
                            stream.feed(b'\x00\x20' * 160)
                await stream.aclose()
            await asyncio.wait_for(state.sockets_idle.wait(), 1)
            assert state.open_sockets == 0 and not state.chat_requests
    asyncio.run(run())


def test_streaming_asr_missing_final_result_cannot_become_a_recognized_input():
    async def run():
        import time

        from behavior.models import AttemptPermit
        from channels.models import RoomTarget, SessionRef
        from dialogue.models import AudioInput

        async with model_servers(incomplete_asr=True) as (backend, state):
            session = SessionRef('p', 'b', 's', RoomTarget('a', 'c'))
            permit = AttemptPermit(session, 1, 'input', time.monotonic()+5)
            stream = backend.open_recognition(AudioInput('input', session, b'', 16000,
                                                         'peer', permit.attempt_id), permit)
            try:
                stream.feed(b"\x00\x20" * 1600)
                stream.finish_input()
                result = await stream.result()
                assert result.status == 'failed' and result.text == ''
                assert not state.chat_requests and not state.text
            finally:
                await stream.aclose()
    asyncio.run(run())
