"""真实来源与HTTP模型验证固定上下文、授权失效和后续任务刷新。"""
import asyncio
import base64
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from support.context_fixtures import prepare_source

from channels.models import OutputSourceError, RoomTarget, SessionRef
from context import create_context_service
from context.models import (
    ContextRef,
    ContextRequest,
    ContextRevision,
    HistoryMessage,
    Observation,
    PreparedContext,
)
from dialogue import audio_output, create_mimo_backend
from operations.models import VoiceAgentSettings


def intent(session, prepared):
    return SimpleNamespace(action_id=str(time.monotonic_ns()), session=session,
        context_version=prepared.version, text='请记住我的喜好', user_key='oopz:member', kind='reply',
        deadline=time.monotonic()+5)


def test_late_model_tool_cannot_write_into_replaced_context(tmp_path):
    async def run():
        service=create_context_service(str(tmp_path/'history.jsonl'),timeout=.01)
        await prepare_source(service.bridge,{'ok':True,'persona':'old','provider':''})
        session=SessionRef('run','binding','room',RoomTarget('area','channel'))
        service.observe(Observation('input',session,'connection',1,'recognized','请记住我的喜好','oopz:member','input'))
        prepared=await service.prepare_context(ContextRequest(session,'oopz:member','input'))
        async def model(request):
            await request.json()
            await prepare_source(service.bridge,{'ok':True,'persona':'new','provider':'memory_companion'})
            return web.json_response({'choices':[{'message':{'tool_calls':[{
                'id':'old-write','type':'function','function':{
                    'name':'astrbot_remember','arguments':'{"content":"old fact"}'}}]}}]})
        app=web.Application()
        app.router.add_post('/chat/completions',model)
        async with TestServer(app) as server:
            backend=create_mimo_backend(VoiceAgentSettings(mimo_api_key='synthetic',mimo_base_url=str(server.make_url('/'))))
            audio=audio_output(backend.generate(intent(session,prepared),prepared,tools=service.tools(prepared)))
            try:
                with pytest.raises(OutputSourceError):
                    _=[frame async for frame in audio.frames()]
                assert service.bridge.status()['pending_writes']==0
                assert not [row for row in service.history.recent() if row['role']=='assistant']
            finally:
                await audio.aclose()
                await backend.aclose()
                await service.close()
    asyncio.run(run())


def test_future_generation_uses_new_prepared_context_without_mutating_old_snapshot():
    async def run():
        captured=[]
        async def model(request):
            body=await request.json()
            if body.get('audio'):
                payload={'choices':[{'delta':{'audio':{'data':base64.b64encode(b'\1\0').decode()}}}]}
                return web.Response(text='data: '+json.dumps(payload)+'\n\ndata: [DONE]\n\n')
            captured.append(body)
            return web.json_response({'choices':[{'message':{'content':'你好'}}]})
        app=web.Application()
        app.router.add_post('/chat/completions',model)
        async with TestServer(app) as server:
            backend=create_mimo_backend(VoiceAgentSettings(mimo_api_key='synthetic',mimo_base_url=str(server.make_url('/'))))
            session=SessionRef('run','binding','room',RoomTarget('area','channel'))
            old=PreparedContext(ContextRef(session,0,'local'),ContextRevision(0),'old',(HistoryMessage('user','old history'),))
            new=PreparedContext(old.context_ref,ContextRevision(1),'new',(HistoryMessage('user','new history'),))
            try:
                for prepared in (old,new):
                    output=audio_output(backend.generate(intent(session,prepared),prepared))
                    _=[frame async for frame in output.frames()]
                assert [[message['content'] for message in body['messages'] if message['role']=='user'] for body in captured] == [
                    ['old history','请记住我的喜好'],['new history','请记住我的喜好']]
                assert old.persona=='old' and old.history[0].content=='old history'
            finally:
                await backend.aclose()
    asyncio.run(run())
