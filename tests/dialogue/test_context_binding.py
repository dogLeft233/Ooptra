"""真实来源桥和本地模型协议共同验证生成不能越过来源代次。"""

import asyncio

import pytest
from aiohttp import web
from support.context_fixtures import prepare_source

from behavior import create_reply_policy
from context import create_context_bridge, create_memory
from dialogue import create_mimo_backend
from dialogue.models import VoiceStreamInterrupted
from operations.models import VoiceAgentSettings


def test_late_model_tool_cannot_write_into_replaced_context(tmp_path):
    async def run():
        bridge = create_context_bridge(timeout=0.01, outbox_path=tmp_path / 'outbox.json')
        await prepare_source(bridge, {'ok': True, 'persona': 'old', 'provider': ''})

        async def model(request):
            await request.json()
            await prepare_source(bridge, {'ok': True, 'persona': 'new',
                                          'provider': 'memory_companion'})
            return web.json_response({'choices': [{'message': {'tool_calls': [{
                'id': 'old-model-write', 'type': 'function', 'function': {
                    'name': 'astrbot_remember', 'arguments': '{"content":"old fact"}',
                },
            }]}}]})

        app = web.Application()
        app.router.add_post('/chat/completions', model)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, '127.0.0.1', 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        backend = create_mimo_backend(
            VoiceAgentSettings(mimo_api_key='synthetic', mimo_base_url=f'http://127.0.0.1:{port}'),
            create_memory(str(tmp_path / 'history.jsonl')), reply_policy=create_reply_policy(),
        )
        backend.context_bridge = bridge
        try:
            with pytest.raises(VoiceStreamInterrupted):
                await asyncio.wait_for(backend.chat('remember this', user_key='oopz:member'), 0.5)
            assert bridge.status()['pending_writes'] == 0
            assert bridge.status()['pending_requests'] == 0
        finally:
            await backend.aclose()
            await runner.cleanup()

    asyncio.run(run())


def test_rebinding_history_changes_future_model_context(tmp_path):
    async def run():
        captured = []

        async def model(request):
            captured.append(await request.json())
            return web.json_response({'choices': [{'message': {'content': 'hello'}}]})

        app = web.Application()
        app.router.add_post('/chat/completions', model)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, '127.0.0.1', 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        old = create_memory(str(tmp_path / 'old.jsonl'))
        new = create_memory(str(tmp_path / 'new.jsonl'))
        old.append('user', 'old history', user_key='member')
        new.append('user', 'new history', user_key='member')
        backend = create_mimo_backend(
            VoiceAgentSettings(mimo_api_key='synthetic', mimo_base_url=f'http://127.0.0.1:{port}'),
            old, reply_policy=create_reply_policy(),
        )
        try:
            backend.bind_history(new)
            assert await backend.chat('current question', user_key='member') == 'hello'
            assert [message['content'] for message in captured[0]['messages']
                    if message['role'] == 'user'] == ['new history', 'current question']
            assert [row['content'] for row in old.recent()] == ['old history']
            assert [row['content'] for row in new.recent()] == ['new history']
        finally:
            await backend.aclose()
            await runner.cleanup()

    asyncio.run(run())
