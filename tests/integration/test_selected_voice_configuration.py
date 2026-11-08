"""选择音色经过真实配置事务与运行时重载，不改开发者配置。"""
import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def test_selected_voice_persists_hot_applies_and_requires_auth(tmp_path, monkeypatch):
    import config
    from app import assembly
    from operations import load_voice_agent_settings
    from webui import config_editor
    from webui.server import WebUIConsole
    from webui.voice_routes import build_voice_routes

    voice = 'qwen-audio-3.1-tts-flash-girl-fixture'
    raw = {'enabled': False, 'backend': 'aliyun_deepseek',
           'memory_path': str(tmp_path / 'memory.jsonl'),
           'aliyun_deepseek': {'tts_voice': 'longanhuan_v3.1'}}
    monkeypatch.setattr(config, 'VOICE_AGENT_CONFIG', raw)
    monkeypatch.setattr(config, 'VOICE_API_CONFIG', {'enabled': False})
    monkeypatch.setattr(config, 'VOICE_AUTO_VISIT_CONFIG', {})
    path = tmp_path / 'config.py'
    path.write_text('VOICE_AGENT_CONFIG = ' + repr(raw) + '\n', encoding='utf-8')
    monkeypatch.setattr(config_editor, 'CONFIG_PATH', str(path))
    monkeypatch.setattr(config_editor, '_runtime_config', lambda: config)

    def load():
        return load_voice_agent_settings(environ={}, local_environment={}, runtime_config=config)
    monkeypatch.setattr(assembly, 'load_voice_agent_settings', load)

    async def run():
        runtime = assembly.VoiceRuntime(state_path=tmp_path / 'visits.json')
        console = WebUIConsole(SimpleNamespace(), SimpleNamespace(), voice_runtime=runtime)
        console._token = 'synthetic-token'
        app = web.Application(middlewares=[console._auth_middleware])
        app.router.add_get('/api/config', console._handle_config_get)
        app.router.add_post('/api/config', console._handle_config_post)
        app.add_routes(build_voice_routes(runtime, with_api_alias=True))
        try:
            async with TestClient(TestServer(app)) as client:
                for endpoint in ('/api/voice/voices', '/voice/voices'):
                    assert (await client.get(endpoint)).status == 401
                    assert (await client.post(endpoint)).status == 401
                headers = {'Authorization': 'Bearer synthetic-token'}
                current = await (await client.get('/api/config', headers=headers)).json()
                response = await client.post('/api/config', headers=headers, json={
                    'expected_revision': current['revision'], 'request_id': 'select-girl',
                    'updates': {'voice': {'aliyun_deepseek.tts_voice': voice}}})
                assert response.status == 200, await response.text()
                assert (await response.json())['application_state'] == 'applied'
                assert runtime.agent.settings.aliyun_tts_voice == voice
                assert runtime.agent.backend.settings.aliyun_tts_voice == voice
                namespace = {}
                exec(path.read_text(), namespace)
                assert namespace['VOICE_AGENT_CONFIG']['aliyun_deepseek']['tts_voice'] == voice
                assert load()[0].aliyun_tts_voice == voice
        finally:
            await runtime.stop()
    asyncio.run(run())
