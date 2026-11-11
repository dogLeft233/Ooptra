"""真实 HTTP 协议与公开后端接口，不替换 JSON 解析或接话判断代码。"""
import asyncio
import json

import pytest
from aiohttp import web

from channels.models import RoomTarget, SessionRef
from context.models import ContextRef, ContextRevision, HistoryMessage, PreparedContext
from dialogue import create_aliyun_backend
from dialogue.models import ControlInput
from operations.models import VoiceAgentSettings


@pytest.mark.parametrize("raw,finish,status,expected", [
    ('{"decision":"addressed"}', "stop", 200, "addressed"),
    ('{"decision":"contribute"}', "stop", 200, "contribute"),
    ('{"decision":"silent"}', "stop", 200, "silent"),
    ('{"decision":true}', "stop", 200, "invalid"),
    ('{"decision":"silent","decision":"addressed"}', "stop", 200, "invalid"),
    ('{"decision":"addressed"}', "length", 200, "invalid"),
    ('   ', "stop", 200, "invalid"),
    ('{}', "stop", 503, "unavailable"),
])
@pytest.mark.parametrize("persona,aliases", [("你是小欧，语音频道的伙伴。", ()),
    ("你是小明，语音频道的伙伴。", ()), ("你是大肥鱼。", ("大肥鱼",))])
def test_public_assessment_parses_formal_json_and_keeps_reasoning_out(raw, finish, status, expected, persona, aliases):
    async def run():
        async def handle(request):
            body = await request.json()
            assert body['response_format'] == {'type': 'json_object'}
            assert body['thinking'] == {'type': 'disabled'}
            assert body['reasoning_effort'] == 'none'
            assert body['max_tokens'] == 128 and body['stream'] is False
            payload = json.loads(body['messages'][-1]['content'])
            assert payload['current_utterance'] == '大飞鱼，在吗？'
            assert payload['bot_persona'] == persona
            assert payload['aliases'] == list(aliases)
            assert payload['history'][-1]['content'] == '我在，刚才讲到曲率。'
            return web.json_response({'choices': [{'finish_reason': finish, 'message': {
                'content': raw, 'reasoning_content': '不可播出的内部思考'}}]}, status=status)
        app = web.Application()
        app.router.add_post('/chat/completions', handle)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, '127.0.0.1', 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        backend = create_aliyun_backend(VoiceAgentSettings(backend='aliyun_deepseek',
            deepseek_api_key='fake', deepseek_base_url=f'http://127.0.0.1:{port}'))
        session = SessionRef('run', 'bind', 'session', RoomTarget('a', 'b'))
        prepared = PreparedContext(ContextRef(session, 0, 'local'), ContextRevision(), persona,
            (HistoryMessage('assistant', '我在，刚才讲到曲率。'),))
        try:
            assert backend.capabilities().semantic_reply
            result = await backend.assess_reply(ControlInput('大飞鱼，在吗？', 'input', session, 'u'),
                prepared, aliases=aliases)
            assert result.decision == expected
            assert result.session == session and result.input_ref == 'input'
        finally:
            await backend.aclose()
            await runner.cleanup()
    asyncio.run(run())
