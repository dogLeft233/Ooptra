"""已保存但组件失败的同ID请求只重试失败应用，不重写文件或重做成功组件。"""
import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.assembly import VoiceRuntime
from app.management import RuntimeManagement
from operations import create_configuration
from tests.test_webui_auth import console_app
from webui import config_editor


@pytest.mark.parametrize('superseded', [False, True])
def test_same_request_retries_failed_voice_fields_without_reapplying_successful_visit_config(tmp_path, monkeypatch, superseded):
    import config
    voice = dict(enabled=False, backend='mimo_cascade', output_volume=50,
                 memory_path=str(tmp_path / 'history.jsonl'))
    visits = {}
    path = tmp_path / 'config.py'
    path.write_text('VOICE_AGENT_CONFIG = ' + repr(voice) + '\nVOICE_AUTO_VISIT_CONFIG = {}\n')
    monkeypatch.setattr(config, 'VOICE_AGENT_CONFIG', voice)
    monkeypatch.setattr(config, 'VOICE_AUTO_VISIT_CONFIG', visits)
    monkeypatch.setattr(config, 'VOICE_API_CONFIG', {'enabled': False})
    monkeypatch.setattr(config_editor, 'CONFIG_PATH', str(path))
    monkeypatch.setattr(config_editor, '_runtime_config', lambda:config)

    async def scenario():
        runtime = VoiceRuntime(state_path=tmp_path / 'visits.json')
        console, app = console_app()
        console._voice_runtime = runtime
        console.set_runtime_control(RuntimeManagement(runtime, SimpleNamespace(), lambda:None, root=tmp_path))
        app.router.add_post('/api/config', console._handle_config_post)
        app.router.add_get('/api/config', console._handle_config_get)
        refreshed, reloaded_visits = [], []
        original_refresh, original_visits = runtime.agent.refresh, runtime.auto_visit.reload
        async def refresh(fields):
            refreshed.append(tuple(fields))
            if len(refreshed) == 1:
                raise OSError('synthetic model reconnect failure')
            return await original_refresh(fields)
        async def reload_visits(value):
            reloaded_visits.append(value)
            await original_visits(value)
        runtime.agent.refresh, runtime.auto_visit.reload = refresh, reload_visits
        store = create_configuration(path=path)
        body = dict(request_id='same-request', expected_revision=store.revision(), updates={
            'voice': {'output_volume': 65}, 'auto_visit': {'defaults': {'join_probability': .4}}})
        headers = {'Authorization':'Bearer shared-password'}
        async with TestClient(TestServer(app)) as client:
            first = await client.post('/api/config', json=body, headers=headers)
            assert first.status == 200
            assert (await first.json())['application_state'] == 'partial'
            saved_stamp = path.stat().st_mtime_ns
            if superseded:
                from operations.models import ConfigurationChange
                await asyncio.to_thread(store.apply_latest, 'newer', ConfigurationChange.from_normalized(
                    {'voice':{'output_volume':70}}, sources={'voice':'VOICE_AGENT_CONFIG'}))
            second = await client.post('/api/config', json=body, headers=headers)
            if superseded:
                assert second.status == 400
                assert len(refreshed) == 1 and len(reloaded_visits) == 1
                assert config.VOICE_AGENT_CONFIG['output_volume'] == 70
                assert '70' in path.read_text()
            else:
                assert second.status == 200
                assert (await second.json())['application_state'] == 'applied'
                assert path.stat().st_mtime_ns == saved_stamp
                assert refreshed == [('output_volume',), ('output_volume',)]
                assert len(reloaded_visits) == 1
            receipt = await client.get('/api/config?request_id=same-request', headers=headers)
            data = (await receipt.json())['result']
            assert data['state'] == ('partial' if superseded else 'applied')
            assert data['failed'] == (['voice'] if superseded else [])
            if not superseded:
                assert set(data['applied']) >= {'voice', 'auto_visit'}
                await client.post('/api/config', json=body, headers=headers)
                assert len(refreshed) == 2 and len(reloaded_visits) == 1
        await runtime.stop()
    asyncio.run(scenario())


def test_voice_save_rejects_busy_config_gate_instead_of_deadlocking_hot_reload(tmp_path, monkeypatch):
    import config
    from operations import configuration_transaction
    from operations.models import ConfigurationChange, OutputVolume
    voice = dict(enabled=False, backend='mimo_cascade', output_volume=50,
                 memory_path=str(tmp_path / 'history.jsonl'))
    path = tmp_path / 'config.py'
    path.write_text('VOICE_AGENT_CONFIG = ' + repr(voice) + '\n')
    monkeypatch.setattr(config, 'VOICE_AGENT_CONFIG', voice)
    monkeypatch.setattr(config, 'VOICE_AUTO_VISIT_CONFIG', {})
    monkeypatch.setattr(config, 'VOICE_API_CONFIG', {'enabled':False})
    async def scenario():
        store = create_configuration(path=path, runtime_config=lambda:config)
        runtime = VoiceRuntime(state_path=tmp_path / 'visits.json', configuration=store)
        voice_locked, allow_voice_save, ui_committed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def save(volume):
            voice_locked.set()
            await allow_voice_save.wait()
            await asyncio.to_thread(store.save_output_volume, OutputVolume(volume))
        runtime.agent.set_output_volume_saver(save)
        spoken = asyncio.create_task(runtime.agent.set_output_controls(volume=40))
        await voice_locked.wait()
        async def ui():
            await asyncio.to_thread(store.apply_latest, 'ui', ConfigurationChange.from_normalized(
                {'voice':{'output_volume':80}}, sources={'voice':'VOICE_AGENT_CONFIG'}))
            ui_committed.set()
            try:
                await asyncio.wait_for(runtime.reload_settings(), .2)
            except asyncio.TimeoutError:
                return 'deadlocked'
            return 'applied'
        ui_task = asyncio.create_task(configuration_transaction(tmp_path, ui))
        await ui_committed.wait()
        allow_voice_save.set()
        voice_result, ui_result = await asyncio.gather(spoken, ui_task)
        assert ui_result == 'applied'
        assert not voice_result['ok']
        assert runtime.agent.settings.output_volume == 80
        assert '80' in path.read_text()
        await runtime.stop()
    asyncio.run(scenario())
