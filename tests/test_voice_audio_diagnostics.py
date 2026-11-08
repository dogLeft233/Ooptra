"""独立模型自检经过真实协议适配，不触碰活跃房间或业务上下文。"""
import asyncio
import base64
import io
import json
import wave

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from support.live_socket import Socket
from test_voice_operations import make_agent


def sample_wav(seconds=.1):
    buffer = io.BytesIO()
    with wave.open(buffer,'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b'\1\0'*int(16000*seconds))
    return buffer.getvalue()


@pytest.mark.parametrize('failure',[None,'asr','intent','tts'])
def test_sample_probe_is_independent_and_reports_individual_stages(tmp_path,failure):
    from app import diagnostics
    async def run():
        requests = []
        async def model(request):
            body = await request.json()
            requests.append(body)
            kind = ('tts' if body.get('audio') else 'asr' if body.get('asr_options') else 'intent')
            if kind == failure:
                return web.json_response({'error':'private upstream body'},status=503)
            if kind == 'tts':
                pcm = base64.b64encode(b'\1\0'*100).decode()
                return web.Response(text='data: '+json.dumps({'choices':[{'delta':{
                    'audio':{'data':pcm}}}]})+'\n\ndata: [DONE]\n\n')
            content = '你好' if kind=='asr' else '{"leave":false,"volume":null}'
            return web.json_response({'choices':[{'message':{'content':content}}]})
        app = web.Application()
        app.router.add_post('/v1/chat/completions',model)
        async with TestServer(app) as server:
            agent = make_agent(tmp_path)
            active = agent.backend
            agent.settings.mimo_api_key = 'synthetic'
            agent.settings.mimo_base_url = str(server.make_url('/v1'))
            result = await diagnostics.diagnose(agent,audio_wav=sample_wav())
            checks = {item['id']:item for item in result['checks']}
            for stage in ('asr','intent','tts'):
                expected = 'skipped' if failure=='asr' and stage=='intent' else 'fail' if failure==stage else 'pass'
                assert checks[stage]['state'] == expected
            assert agent.backend is active
            assert not agent.status()['joined'] and not agent.memory.recent()
    asyncio.run(run())


@pytest.mark.parametrize('silent_output',[False,True])
def test_live_sample_probe_uses_separate_connections_for_input_and_output(tmp_path,monkeypatch,silent_output):
    from app import diagnostics
    from dialogue import create_gemini_backend
    async def run():
        sockets=[]
        class ProbeSocket(Socket):
            async def send(self,text):
                await super().send(text)
                data=self.sent[-1].get('realtimeInput',{})
                if data.get('audioStreamEnd'):
                    await self.incoming.put({'serverContent':{'inputTranscription':{'text':'请先离开'}}})
                    await self.incoming.put({'toolCall':{'functionCalls':[{
                        'id':'leave','name':'leave_voice_room','args':{}}]}})
                elif data.get('text'):
                    content={'turnComplete':True}
                    if not silent_output:
                        content['modelTurn']={'parts':[{'inlineData':{
                            'data':base64.b64encode(b'\1\0'*100).decode(),
                            'mimeType':'audio/pcm;rate=24000'}}]}
                    await self.incoming.put({'serverContent':content})
        async def connect(*args,**kwargs):
            socket=ProbeSocket()
            sockets.append(socket)
            return socket
        monkeypatch.setattr(diagnostics,'create_backend',lambda settings:
            create_gemini_backend(settings,connector=connect))
        agent=make_agent(tmp_path)
        agent.settings.backend='gemini_live'
        agent.settings.gemini_api_key='synthetic'
        active=agent.backend
        result=await diagnostics.diagnose(agent,audio_wav=sample_wav())
        checks={item['id']:item for item in result['checks']}
        assert checks['asr']['state']=='pass'
        assert checks['intent']['state']=='pass'
        assert checks['tts']['state']==('fail' if silent_output else 'pass')
        assert len(sockets)==2 and all(socket.closed for socket in sockets)
        assert agent.backend is active and not agent.memory.recent()
    asyncio.run(run())


def test_cancelling_live_output_probe_closes_both_disposable_connections(tmp_path,monkeypatch):
    from app import diagnostics
    from dialogue import create_gemini_backend
    async def run():
        sockets=[]
        started=asyncio.Event()
        class ProbeSocket(Socket):
            async def send(self,text):
                await super().send(text)
                data=self.sent[-1].get('realtimeInput',{})
                if data.get('audioStreamEnd'):
                    await self.incoming.put({'serverContent':{'inputTranscription':{'text':'你好'},'turnComplete':True}})
                elif data.get('text'):
                    started.set()
        async def connect(*args,**kwargs):
            socket=ProbeSocket()
            sockets.append(socket)
            return socket
        monkeypatch.setattr(diagnostics,'create_backend',lambda settings:
            create_gemini_backend(settings,connector=connect))
        agent=make_agent(tmp_path)
        agent.settings.backend='gemini_live'
        agent.settings.gemini_api_key='synthetic'
        task=asyncio.create_task(diagnostics.diagnose(agent,audio_wav=sample_wav()))
        await asyncio.wait_for(started.wait(),1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(sockets)==2 and all(socket.closed for socket in sockets)
    asyncio.run(run())


@pytest.mark.parametrize('data',[b'garbage',sample_wav(10.1),b'x'*(2*1024*1024+1)],ids=['invalid','too-long','too-large'])
def test_invalid_or_long_audio_never_constructs_network_backend(tmp_path,monkeypatch,data):
    from app import diagnostics
    async def run():
        agent=make_agent(tmp_path)
        agent.settings.mimo_api_key='synthetic'
        monkeypatch.setattr(diagnostics,'create_backend',lambda *args,**kwargs:pytest.fail('invalid audio sent to model'))
        with pytest.raises(ValueError):
            await diagnostics.diagnose(agent,audio_wav=data)
    asyncio.run(run())
