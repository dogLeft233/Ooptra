"""用真实本地 HTTP/WS 验证复刻目录与指定音色合成，隔离真实供应商。"""
import asyncio
import base64
import io
import wave
from types import SimpleNamespace

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from dialogue import create_aliyun_backend, create_voice_library
from dialogue.models import VoiceSample
from operations.models import VoiceAgentSettings
from webui.voice_library_routes import voice_library_handlers

MODEL = "qwen-audio-3.1-tts-flash"
VOICE = MODEL + "-girl-fixture"


def sample_wav(seconds=10, rate=24000):
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b'\x01\x00' * int(seconds * rate))
    return out.getvalue()


@pytest.mark.parametrize('prefix,mime,audio', [
    ('中文', 'audio/wav', sample_wav()), ('a' * 11, 'audio/wav', sample_wav()),
    ('girl', 'audio/wav', b'broken'), ('girl', 'audio/wav', sample_wav(61)),
    ('girl', 'audio/wav', sample_wav(4)), ('girl', 'audio/wav', sample_wav(rate=8000)),
    ('girl', 'audio/wav', sample_wav()[:-2]), ('girl', 'audio/mpeg', b''),
    ('girl', 'audio/mpeg', b'x' * (10 * 1024 * 1024 + 1)),
    ('girl', 'text/plain', b'not audio'),
], ids=['non_ascii_name', 'long_name', 'broken_wav', 'long_wav', 'short_wav',
        'low_sample_rate', 'truncated_wav', 'empty_audio', 'oversized_audio', 'unsupported_type'])
def test_sample_rejects_invalid_input_before_upload(prefix, mime, audio):
    with pytest.raises(ValueError):
        VoiceSample(prefix, mime, audio)


def test_create_list_check_and_speak_selected_voice():
    async def run():
        requests = []
        state = {'status': 'DEPLOYING', 'target': MODEL}
        spoken = []
        async def cloud(request):
            assert request.headers['Authorization'] == 'Bearer synthetic-key'
            body = await request.json()
            assert body['model'] == 'voice-enrollment'
            fields = body['input']
            requests.append(fields)
            if fields['action'] == 'create_voice':
                assert fields['target_model'] == MODEL
                assert fields['prefix'] == 'girl'
                assert base64.b64decode(fields['audio'].split(',')[1]) == sample_wav()
                return web.json_response({'output': {'voice_id': VOICE}})
            if fields['action'] == 'list_voice':
                return web.json_response({'output': {'voice_list': [
                    {'voice_id': VOICE, 'status': state['status']},
                    {'voice_id': 'qwen-audio-3.1-tts-plus-other', 'status': 'OK'}]}})
            return web.json_response({'output': {'target_model': state['target'], 'status': state['status']}})

        async def tts(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            start = await ws.receive_json()
            spoken.append(start['payload']['parameters']['voice'])
            task = start['header']['task_id']
            await ws.send_json({'header': {'event': 'task-started', 'task_id': task}})
            async for message in ws:
                if message.type == web.WSMsgType.TEXT and 'finish-task' in message.data:
                    await ws.send_bytes(b'\x01\x00' * 240)
                    await ws.send_json({'header': {'event': 'task-finished', 'task_id': task}})
                    break
            await ws.close()
            return ws

        app = web.Application(client_max_size=15 * 1024 * 1024)
        app.router.add_post('/cloud', cloud)
        app.router.add_get('/tts', tts)
        async with TestServer(app) as server:
            settings = VoiceAgentSettings(backend='aliyun_deepseek', dashscope_api_key='synthetic-key',
                                         dashscope_ws_url=str(server.make_url('/tts')).replace('http:', 'ws:'))
            service = create_voice_library(endpoint=str(server.make_url('/cloud')))
            runtime = SimpleNamespace(agent=SimpleNamespace(settings=settings))
            ui = web.Application(client_max_size=3 * 1024 * 1024)
            for method, path, handler in voice_library_handlers(runtime, service):
                ui.router.add_route(method, path, handler)
            async with TestClient(TestServer(ui)) as client:
                form = FormData()
                form.add_field('prefix', 'girl')
                form.add_field('audio', sample_wav(), filename='sample.wav', content_type='audio/wav')
                response = await client.post('/voice/voices', data=form)
                assert response.status == 200, await response.text()
                assert (await response.json())['voice']['voice_id'] == VOICE
                response = await client.get('/voice/voices')
                data = await response.json()
                assert [v['voice_id'] for v in data['voices']] == ['longanhuan_v3.1', VOICE]
                response = await client.post('/voice/voices/check', json={'voice_id': VOICE})
                assert response.status == 400  # 创建不等于可用
                state['status'] = 'OK'
                response = await client.post('/voice/voices/check', json={'voice_id': VOICE})
                assert (await response.json())['voice']['status'] == 'OK'
                state['target'] = 'qwen-audio-3.1-tts-plus'
                response = await client.post('/voice/voices/check', json={'voice_id': VOICE})
                assert response.status == 400  # 跨模型不可复用
                state['target'] = MODEL
                assert settings.aliyun_tts_voice == 'longanhuan_v3.1'  # 目录不暗中切换
                settings.aliyun_tts_voice = VOICE
                backend = create_aliyun_backend(settings)
                try:
                    pcm, rate = await backend.tts('你好')
                    assert pcm and rate == 24000
                    assert spoken == [VOICE]
                finally:
                    await backend.aclose()
                response = await client.get('/voice/voices?page=-1')
                assert response.status == 400
            assert len([r for r in requests if r['action'] == 'create_voice']) == 1
    asyncio.run(run())


def test_cloud_failure_is_safe_and_create_is_not_retried(caplog):
    async def run():
        calls = []
        async def cloud(request):
            calls.append(await request.json())
            return web.json_response({'message': 'synthetic-secret-and-audio'}, status=403)
        app = web.Application()
        app.router.add_post('/', cloud)
        async with TestServer(app) as server:
            service = create_voice_library(endpoint=str(server.make_url('/')))
            settings = VoiceAgentSettings(backend='aliyun_deepseek', dashscope_api_key='synthetic')
            with pytest.raises(ValueError, match='HTTP 403') as error:
                await service.create(settings, VoiceSample('girl', 'audio/wav', sample_wav()))
            assert 'synthetic-secret' not in str(error.value)
            assert len(calls) == 1
            assert caplog.text.count("event=dialogue.voice_library.failed") == 1
            assert "synthetic-secret" not in caplog.text
    asyncio.run(run())


def test_cancelled_creation_releases_connection_and_allows_next_request():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        requests = []
        async def cloud(request):
            requests.append(await request.json())
            started.set()
            await release.wait()
            return web.json_response({'output': {'voice_id': VOICE}})
        app = web.Application()
        app.router.add_post('/', cloud)
        async with TestServer(app) as server:
            service = create_voice_library(endpoint=str(server.make_url('/')))
            settings = VoiceAgentSettings(backend='aliyun_deepseek', dashscope_api_key='synthetic')
            sample = VoiceSample('girl', 'audio/wav', sample_wav())
            task = asyncio.create_task(service.create(settings, sample))
            try:
                await asyncio.wait_for(started.wait(), 1)
                with pytest.raises(ValueError, match='正在创建'):
                    await service.create(settings, sample)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                release.set()
                assert (await service.create(settings, sample)).voice_id == VOICE
                assert len(requests) == 2
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
