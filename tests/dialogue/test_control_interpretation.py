"""控制分类由公开端口保留解析事实，不与概率、聊天或TTS混合。"""
import asyncio

import pytest
from aiohttp import web

from channels.models import RoomTarget, SessionRef
from dialogue import create_mimo_backend
from dialogue.models import ControlInput
from operations.models import VoiceAgentSettings


@pytest.mark.parametrize("raw,status,leave", [
    ('{"leave": true, "volume": null}', 'candidate', True),
    ('```json\n{"leave": false,"volume":null}\n```', 'candidate', False),
    ('{"leave": "true", "volume": null}', 'invalid', False),
    ('{"reply":"模型草稿"}', 'invalid', False),
    ('not JSON', 'invalid', False),
    ('{"leave":true,"volume":{"action":"mute"}}', 'invalid', False),
])
def test_classification_preserves_valid_control_and_distinguishes_invalid_response(raw, status, leave):
    async def scenario():
        requests = []
        async def model(request):
            requests.append(await request.json())
            return web.json_response({'choices':[{'message':{'content':raw}}]})
        app = web.Application()
        app.router.add_post('/chat/completions', model)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner,'127.0.0.1',0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        backend = create_mimo_backend(VoiceAgentSettings(mimo_api_key='synthetic',
            mimo_base_url=f'http://127.0.0.1:{port}'))
        session = SessionRef('run','binding','room',RoomTarget('area','channel'))
        try:
            result = await backend.interpret_control(ControlInput('你先出去吧','input',session))
            assert result.status == status
            assert (result.candidate.leave if result.candidate is not None else False) is leave
            assert len(requests) == 1
            assert requests[0]['response_format'] == {'type':'json_object'}
            assert '不要出去' in requests[0]['messages'][0]['content']
            assert not requests[0].get('audio')
        finally:
            await backend.aclose()
            await runner.cleanup()
    asyncio.run(scenario())


def test_failed_classification_is_unavailable_instead_of_permitting_ordinary_reply(caplog):
    async def scenario():
        async def model(request):
            return web.json_response({'error':'secret upstream body'}, status=503)
        app = web.Application()
        app.router.add_post('/chat/completions',model)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner,'127.0.0.1',0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        backend = create_mimo_backend(VoiceAgentSettings(mimo_api_key='synthetic',
            mimo_base_url=f'http://127.0.0.1:{port}'))
        session = SessionRef('run','binding','room',RoomTarget('area','channel'))
        try:
            result = await backend.interpret_control(ControlInput('请出去','input',session))
            assert result.status == 'unavailable' and result.candidate is None
            assert 'secret upstream' not in caplog.text
            assert 'secret' not in result.reason
        finally:
            await backend.aclose()
            await runner.cleanup()
    asyncio.run(scenario())
