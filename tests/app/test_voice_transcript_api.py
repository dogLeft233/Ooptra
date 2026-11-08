"""经过真实鉴权和HTTP路由查询持久化的逐句记录。"""
import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.http import VoiceApiServer
from channels.models import RoomTarget, SessionRef
from context import create_context_service
from context.models import VoiceTrace
from operations.models import VoiceApiSettings
from webui.voice_routes import build_voice_routes


def test_authenticated_paged_transcripts_and_name_fallback(tmp_path):
    async def run():
        context = create_context_service(str(tmp_path / 'memory.jsonl'))
        session = SessionRef('run', 'bind', 'session', RoomTarget('a', 'b'))
        for i in range(3):
            context.record_voice_trace(VoiceTrace(session, str(i), 'local-user', 'finished',
                text='大肥鱼你在吗？' if i == 2 else '我继续算题',
                decision='speak' if i == 2 else 'silent', reason='keyword' if i == 2 else 'semantic_silent',
                outcome='completed' if i == 2 else 'semantic_silent'))
        runtime = SimpleNamespace(agent=SimpleNamespace(context_service=context))
        api = VoiceApiServer(runtime, VoiceApiSettings(enabled=True, token='fixture'))
        app = web.Application()
        app.add_routes(build_voice_routes(runtime, with_api_alias=True, wrap=api._wrap))
        try:
            async with TestClient(TestServer(app)) as client:
                response = await client.get('/api/voice/transcripts')
                assert response.status == 401
                headers = {'Authorization': 'Bearer fixture'}
                response = await client.get('/api/voice/transcripts?limit=2&area=a&channel=b', headers=headers)
                page = await response.json()
                assert response.headers['Cache-Control'] == 'no-store'
                assert page['total'] == 3 and len(page['rows']) == 2
                assert page['rows'][0]['speaker_name'] == 'local-user'
                assert page['rows'][0]['text'] == '大肥鱼你在吗？'
                assert page['rows'][0]['reason'] == 'keyword'
                assert page['rows'][1]['reason'] == 'semantic_silent'
                response = await client.get('/voice/transcripts?limit=2&before='+str(page['next_before']), headers=headers)
                older = await response.json()
                assert len(older['rows']) == 1 and older['next_before'] is None
                for query in ['limit=0', 'limit=101', 'limit=true', 'before=-1', 'before=0', 'area=a']:
                    response = await client.get('/voice/transcripts?'+query, headers=headers)
                    assert response.status == 400
        finally:
            await context.close()
    asyncio.run(run())
