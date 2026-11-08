"""维护端口不能把业务停止失败报告成完整静默。"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.management import RuntimeManagement
from operations.models import HandoffRef


def test_real_voice_runtime_reports_cleanup_failure_and_still_closes_other_resources(tmp_path, monkeypatch):
    import config
    from app.assembly import VoiceRuntime
    monkeypatch.setattr(config, 'VOICE_AGENT_CONFIG', {
        'enabled': False, 'backend': 'mimo_cascade', 'memory_path': str(tmp_path / 'history.jsonl')})
    monkeypatch.setattr(config, 'VOICE_AUTO_VISIT_CONFIG', {})
    monkeypatch.setattr(config, 'VOICE_API_CONFIG', {'enabled': False})

    async def scenario():
        runtime = VoiceRuntime(state_path=tmp_path / 'visits.json')
        await runtime.start()
        original_stop = runtime.agent.stop
        fail = True
        calls = []
        async def agent_stop():
            calls.append('agent')
            if fail:
                raise RuntimeError('synthetic incomplete channel shutdown')
            await original_stop()
        api_stop = runtime.api.stop
        async def api():
            calls.append('api')
            await api_stop()
        runtime.agent.stop, runtime.api.stop = agent_stop, api
        controller = SimpleNamespace(stop=AsyncMock())
        exits = []
        management = RuntimeManagement(runtime, controller, lambda: exits.append(True), root=tmp_path)
        result = await management.quiesce('job', time.monotonic() + 2)
        assert not result.complete and result.pending == ('voice',)
        assert result.closed == ('channels',)
        assert calls == ['agent', 'api']
        assert not management.business_allowed
        assert not (await management.request_exit(HandoffRef('job', 1, 'token'))).accepted
        assert exits == []
        fail = False
        result = await management.quiesce('job', time.monotonic() + 2)
        assert result.complete
        assert calls == ['agent', 'api', 'agent', 'api']
        assert not (await management.request_exit(HandoffRef('job', 1, 'token'))).accepted
        import os

        from operations import write_maintenance_record
        write_maintenance_record(tmp_path / 'data/maintenance/job.json', dict(
            id='job', phase='awaiting_exit', revision=1, handoff_token='token', service_pid=os.getpid()))
        assert not (await management.request_exit(HandoffRef('job', 1, 'forged'))).accepted
        assert (await management.request_exit(HandoffRef('job', 1, 'token'))).accepted
        assert exits == [True]
    asyncio.run(scenario())


def test_runtime_apply_report_preserves_hot_reload_failure():
    async def scenario():
        voice = SimpleNamespace(reload_settings=AsyncMock(return_value={
            'changed': ['output_volume'], 'hot_reload_failed': True, 'restart_keys': []}))
        management = RuntimeManagement(voice, SimpleNamespace(), lambda: None)
        result = await management.apply_config('revision', SimpleNamespace())
        assert result.failed == ('voice',)
        assert result.applied == ()
    asyncio.run(scenario())
