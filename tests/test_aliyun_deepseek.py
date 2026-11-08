"""Local protocol servers exercise streaming, cancellation and secret loading."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from support.channel_fixtures import establish_room
from test_voice_operations import make_agent

from voice_agent import settings as settings_module
from voice_agent.backends.aliyun_deepseek import AliyunDeepSeekBackend
from voice_agent.backends.base import VoiceReply, VoiceStreamError, VoiceStreamInterrupted
from voice_agent.preview import EphemeralMemory
from voice_agent.settings import VoiceAgentSettings, load_voice_agent_settings


def test_local_keys_do_not_expand_shell_or_override_process_env(tmp_path, monkeypatch):
    path = tmp_path / '.env.local'
    path.write_text('DASHSCOPE_API_KEY="local-${UNSET}"\nDEEPSEEK_API_KEY=local-deepseek\n')
    monkeypatch.setattr(settings_module, 'project_path', lambda *args: str(path))
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'process-deepseek')
    settings, _ = load_voice_agent_settings()
    assert settings.dashscope_api_key == 'local-${UNSET}'
    assert settings.deepseek_api_key == 'process-deepseek'
    assert 'local-${UNSET}' not in repr(settings)
    assert 'process-deepseek' not in repr(settings)
    assert 'DASHSCOPE_API_KEY' not in settings_module.os.environ


@asynccontextmanager
async def model_servers(*, fail_asr=False, truncate=False, control=False):
    state = SimpleNamespace(
        text=[], chat_requests=[], audio_seen=asyncio.Event(), llm_finished=False,
        open_sockets=0, asr_bytes=bytearray(),
    )

    async def websocket(request):
        assert request.headers['Authorization'] == 'Bearer fake-dashscope'
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state.open_sockets += 1
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
    backend = AliyunDeepSeekBackend(settings, EphemeralMemory())
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

            reply = await backend.handle_utterance_stream(
                b'\x01\x00' * 24000, sample_rate=24000,
                user_key='oopz:member', channel_key='area/channel', on_audio=sink,
            )
            assert reply.user_text == '你好'
            assert reply.text == '你好。再见。'
            assert state.text == ['你好。', '再见。']
            assert reply.pcm16 == bytes(output)
            assert len(state.asr_bytes) == 32000  # Resampled from 24 kHz to 16 kHz.
            assert len(state.chat_requests) == 2  # Control classification then speech.
            assert backend._tts_task is None and backend._audio_sink is None

    asyncio.run(run())


def test_control_request_never_opens_tts():
    async def run():
        async with model_servers(control=True) as (backend, state):
            sink = AsyncMock()
            reply = await backend.handle_utterance_stream(
                b'\x01\x00' * 16000, sample_rate=16000,
                user_key='member', channel_key='channel', on_audio=sink,
            )
            assert reply.raw['voice_control'] == 'leave'
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
                        'truncated_sse': VoiceStreamError}[interruption]
            with pytest.raises(expected):
                await backend.handle_utterance_stream(
                    b'\x01\x00' * 16000, sample_rate=16000,
                    user_key='member', channel_key='channel', on_audio=sink,
                )
            assert len(state.chat_requests) == 2
            assert backend._tts_task is None and backend._texts is None
            assert backend._audio_sink is None

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
    async def run():
        agent = make_agent(tmp_path)
        await establish_room(agent)
        pushed = []

        async def push(pcm, rate, **kwargs):
            pushed.append((pcm, kwargs.get('finish', False)))
            return {'ok': True}

        agent.duplex.push_tts_pcm = push
        agent.duplex.stop_tts = AsyncMock(return_value={'ok': True})

        class Backend:
            async def handle_utterance_stream(self, pcm, *, on_audio, **kwargs):
                await on_audio(b'\x01\x00', 24000)
                if invalidate:
                    agent._operation_epoch += 1
                    await on_audio(b'\x02\x00', 24000)
                return VoiceReply(user_text='你好', text='收到', pcm16=b'\x01\x00')

        agent.backend = Backend()
        await agent._handle_utterance('member', b'\x00\x00', 16000)
        assert [pcm for pcm, finish in pushed if not finish] == [b'\x01\x00']
        assert any(finish for _, finish in pushed) is not invalidate
        assert agent.turns == 1
        if not invalidate:
            assert not agent._speaking

    asyncio.run(run())


def test_barge_in_cancels_generation_without_cancelling_room_worker(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await establish_room(agent)
        agent.duplex.push_tts_pcm = AsyncMock(return_value={'ok': True})
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class Backend:
            async def handle_utterance_stream(self, pcm, *, on_audio, **kwargs):
                await on_audio(b'\x01\x00', 24000)
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        agent.backend = Backend()
        worker = asyncio.create_task(agent._handle_utterance('member', b'\x00\x00', 16000))
        await started.wait()
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
            async def handle_utterance_stream(self, *args, **kwargs):
                return VoiceReply()

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
