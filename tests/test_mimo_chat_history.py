"""生成任务使用固定历史；模型净化失败不发声、不提交草稿。"""
import asyncio
import base64
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp import web

from channels.models import OutputSourceError, RoomTarget, SessionRef
from context import create_memory
from context.models import ContextRef, ContextRevision, HistoryMessage, PreparedContext
from dialogue import audio_output, create_mimo_backend
from operations.models import VoiceAgentSettings


@pytest.mark.parametrize('history,model_reply,expected', [
    ('previous', '你好。', '你好。'),
    ('拖'*150, '你好。', '你好。'),
    ('第一行\n第二行', '你好。', '你好。'),
    ('previous', 'The user is just saying Hello repeatedly. I should respond in a cute way.', ''),
    ('previous', '还行，出门带把伞。', '还行，出门带把伞。'),
])
def test_generation_reads_fixed_context_without_duplicating_question_or_recording_draft(tmp_path, history, model_reply, expected):
    async def run():
        requests = []
        pcm = b'\1\0' * 40
        async def model(request):
            body = await request.json()
            requests.append(body)
            if body.get('audio'):
                return web.Response(text='data: '+json.dumps({'choices':[{'delta':{
                    'audio':{'data':base64.b64encode(pcm).decode()}}}]})+'\n\ndata: [DONE]\n\n')
            return web.json_response({'choices':[{'message':{'content':model_reply}}]})
        app = web.Application()
        app.router.add_post('/chat/completions', model)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner,'127.0.0.1',0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        memory = create_memory(str(tmp_path/'history.jsonl'))
        memory.append('user','unrelated global history',user_key='member')
        backend = create_mimo_backend(VoiceAgentSettings(mimo_api_key='synthetic',
            mimo_base_url=f'http://127.0.0.1:{port}'))
        session = SessionRef('run','bind','room',RoomTarget('a','c'))
        prepared = PreparedContext(ContextRef(session,0,'local'),ContextRevision(4),
            '固定人格',(HistoryMessage('user','previous question'), HistoryMessage('assistant',history)))
        intent = SimpleNamespace(action_id='action',session=session,context_version=prepared.version,
            text='current question',user_key='member',kind='reply',deadline=time.monotonic()+5)
        audio = audio_output(backend.generate(intent,prepared))
        try:
            if expected:
                frames = [frame async for frame in audio.frames()]
                assert b''.join(frame.pcm16 for frame in frames) == pcm
                assert audio.evidence().text == expected
            else:
                with pytest.raises(OutputSourceError):
                    _ = [frame async for frame in audio.frames()]
                assert audio.evidence().status == 'failed'
                assert len(requests) == 1, '拒绝推理泄漏时不能调用TTS'
            messages = requests[0]['messages']
            assert messages[0]['content'].startswith('固定人格')
            assert [row['content'] for row in messages if row['role']=='user'] == ['previous question','current question']
            assistant = next(row['content'] for row in messages if row['role']=='assistant')
            assert len(assistant) <= 46 and '\n' not in assistant
            if '\n' in history:
                assert assistant == '第一行，第二行'
            assert [row['content'] for row in memory.recent()] == ['unrelated global history']
        finally:
            await audio.aclose()
            await backend.aclose()
            await runner.cleanup()
    asyncio.run(run())
