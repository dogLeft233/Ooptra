"""WebUI 改配置必须**真正生效**，而不是只写进 config.py 的字典（B4）。

旧行为：``_sync_runtime`` 只改 config 模块的 dict；``VoiceRuntime`` 里
启动时构造的 dataclass 快照（``agent.settings`` / ``backend.settings`` 与
它本是同一个对象）永不刷新，于是「保存成功但不生效、必须重启」。

关键机制：``runtime.agent_settings is agent.settings is backend.settings``，
所以 ``apply_settings`` 就地写回一次，三处同时可见。
"""

from __future__ import annotations

import asyncio

import pytest

from app import assembly as runtime_module
from app.voice import VoiceCoordinator
from operations import apply_settings
from operations.models import VoiceAgentSettings, VoiceApiSettings


def _settings(tmp_path, **overrides) -> VoiceAgentSettings:
    base = {
        "enabled": False,
        "backend": "gemini_live",
        "gemini_api_key": "k",
        "gemini_voice": "Kore",
        "memory_path": str(tmp_path / "voice_memory.jsonl"),
    }
    base.update(overrides)
    return VoiceAgentSettings(**base)


# ----------------------------------------------------------------------
# apply_settings 本身
# ----------------------------------------------------------------------


def test_apply_settings_writes_in_place_and_reports_changes(tmp_path) -> None:
    target = _settings(tmp_path)
    fresh = _settings(tmp_path, gemini_voice="Puck", memory_max_turns=7)

    changed = apply_settings(target, fresh)

    assert target.gemini_voice == "Puck"
    assert target.memory_max_turns == 7
    assert changed == ["gemini_voice", "memory_max_turns"]


def test_apply_settings_returns_empty_when_nothing_changed(tmp_path) -> None:
    target = _settings(tmp_path)
    assert apply_settings(target, _settings(tmp_path)) == []


# ----------------------------------------------------------------------
# 三个持有者是同一个对象
# ----------------------------------------------------------------------


def test_runtime_agent_and_backend_share_one_settings_object(tmp_path) -> None:
    settings = _settings(tmp_path)
    agent = VoiceCoordinator(settings, VoiceApiSettings())

    assert agent.settings is settings
    assert agent.backend.settings is settings

    # 所以改「runtime 的那份」就等于改 backend 那份
    apply_settings(settings, _settings(tmp_path, gemini_voice="Puck"))
    assert agent.backend.settings.gemini_voice == "Puck"


# ----------------------------------------------------------------------
# VoiceRuntime.reload_settings
# ----------------------------------------------------------------------


def _make_runtime(monkeypatch: pytest.MonkeyPatch, agent_cfg, api_cfg=None) -> runtime_module.VoiceRuntime:
    api_cfg = api_cfg or VoiceApiSettings()
    monkeypatch.setattr(
        runtime_module, "load_voice_agent_settings", lambda: (agent_cfg, api_cfg)
    )
    return runtime_module.VoiceRuntime()


def test_reload_settings_propagates_to_running_objects(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    rt = _make_runtime(monkeypatch, _settings(tmp_path))
    assert rt.agent.backend.settings.gemini_voice == "Kore"

    fresh = _settings(tmp_path, gemini_voice="Puck")
    monkeypatch.setattr(
        runtime_module, "load_voice_agent_settings", lambda: (fresh, VoiceApiSettings())
    )
    result = asyncio.run(rt.reload_settings())

    assert result["changed"] == ["gemini_voice"]
    # 就地写回：三处同时可见
    assert rt.agent_settings.gemini_voice == "Puck"
    assert rt.agent.settings.gemini_voice == "Puck"
    assert rt.agent.backend.settings.gemini_voice == "Puck"
    assert result["restart_keys"] == []


def test_reply_controls_reload_reaches_backend_without_replacing_it(monkeypatch, tmp_path):
    rt = _make_runtime(monkeypatch, _settings(tmp_path))
    original_backend = rt.agent.backend
    fresh = _settings(tmp_path, reply_probability_percent=0, force_reply_keywords=["ooptra"],
                      voice_leave_enabled=False)
    monkeypatch.setattr(
        runtime_module, "load_voice_agent_settings", lambda: (fresh, VoiceApiSettings())
    )
    result = asyncio.run(rt.reload_settings())
    assert rt.agent.backend is original_backend
    assert rt.agent.backend.settings.reply_probability_percent == 0
    assert rt.agent.backend.settings.force_reply_keywords == ["ooptra"]
    assert rt.agent.backend.settings.voice_leave_enabled is False
    assert set(result["changed"]) == {"reply_probability_percent", "force_reply_keywords", "voice_leave_enabled"}
    assert not result["restart_keys"]


def test_reload_settings_rebuilds_vad_when_tuning_changes(monkeypatch, tmp_path):
    rt = _make_runtime(monkeypatch, _settings(tmp_path))
    def feed(pcm):
        audio = rt.agent.ingress.accept('member', pcm, 16000)
        assert audio is not None
        return rt.agent.ingress.cut(audio)
    spoken = b'\0\x20' * (600 * 16)
    short_tail = b'\0\0' * (1300 * 16)
    assert feed(spoken + short_tail).status == 'ready'
    fresh = _settings(tmp_path, silence_ms=2000, max_utterance_ms=9000)
    monkeypatch.setattr(runtime_module, 'load_voice_agent_settings', lambda: (fresh, VoiceApiSettings()))
    result = asyncio.run(rt.reload_settings())
    assert 'silence_ms' in result['changed']
    assert feed(spoken + short_tail).status == 'accumulating'
    assert feed(b'\0\0' * (1000 * 16)).status == 'ready'

def test_reload_settings_rebuilds_memory_and_rewires_backend(monkeypatch, tmp_path):
    import base64
    import json
    import time

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from behavior.models import SpeechIntent
    from channels.models import ActionOutcome, RoomTarget, SessionRef
    from context import create_memory
    from context.models import ConfirmedTurn, ContextRequest, OutputRecord
    from dialogue import audio_output

    async def run():
        captured = []
        async def model(request):
            body = await request.json()
            if 'audio' in body:
                event = {'choices': [{'delta': {'audio': {'data': base64.b64encode(b'\1\0' * 20).decode()}}}]}
                return web.Response(text='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n', content_type='text/event-stream')
            captured.append(body)
            return web.json_response({'choices': [{'message': {'content': 'hello'}}]})
        app = web.Application()
        app.router.add_post('/chat/completions', model)
        async with TestServer(app) as server:
            options = dict(backend='mimo_cascade', mimo_api_key='synthetic',
                           mimo_base_url=str(server.make_url('/')).rstrip('/'))
            rt = _make_runtime(monkeypatch, _settings(tmp_path, **options))
            original_history_path = rt.agent.settings.memory_path
            session = SessionRef('run', 'binding', 'session', RoomTarget('area', 'room'))
            async def record(text, identity):
                prepared = await rt.agent.context_service.prepare_context(ContextRequest(session, 'member'))
                rt.agent.context_service.record_output(OutputRecord(identity, session,
                    ConfirmedTurn(text, '', 'failed', 'member'),
                    ActionOutcome(identity, 'failed', session), prepared.context_ref))
            await record('old history', 'old')
            fresh = _settings(tmp_path, memory_path=str(tmp_path / 'other.jsonl'), **options)
            monkeypatch.setattr(runtime_module, 'load_voice_agent_settings', lambda: (fresh, VoiceApiSettings()))
            try:
                assert 'memory_path' in (await rt.reload_settings())['changed']
                await record('new history', 'new')
                prepared = await rt.agent.context_service.prepare_context(ContextRequest(session, 'member'))
                intent = SpeechIntent('current', 'current', session, 'current', prepared.version,
                    time.monotonic() + 5, 'current', 'member')
                audio = audio_output(rt.agent.backend.generate(intent, prepared,
                    tools=rt.agent.context_service.tools(prepared)))
                assert b''.join([frame.pcm16 async for frame in audio.frames()]) == b'\1\0' * 20
                assert [message['content'] for message in captured[0]['messages']
                        if message['role'] == 'user'] == ['new history', 'current']
                assert [row['content'] for row in create_memory(original_history_path).recent()] == ['old history']
            finally:
                await rt.stop()
    asyncio.run(run())

def test_reload_settings_switches_backend_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    rt = _make_runtime(monkeypatch, _settings(tmp_path, backend="gemini_live"))
    old_backend = rt.agent.backend

    fresh = _settings(tmp_path, backend="mimo_cascade", mimo_api_key="x")
    monkeypatch.setattr(
        runtime_module, "load_voice_agent_settings", lambda: (fresh, VoiceApiSettings())
    )
    result = asyncio.run(rt.reload_settings())

    assert "backend" in result["changed"]
    assert rt.agent.backend is not old_backend
    assert rt.agent.live_mode is False, "切到级联后端后 live_mode 没更新"
    assert any("后端" in note for note in result["notes"])


def test_reload_settings_reports_restart_for_api_port_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    rt = _make_runtime(monkeypatch, _settings(tmp_path))

    fresh_api = VoiceApiSettings(enabled=True, port=9999, token="t")
    monkeypatch.setattr(
        runtime_module,
        "load_voice_agent_settings",
        lambda: (_settings(tmp_path), fresh_api),
    )
    result = asyncio.run(rt.reload_settings())

    # host/port 不能热改（socket 已绑定），必须如实告诉用户要重启
    assert "port" in result["restart_keys"]
    assert any("重启" in note for note in result["notes"])
    # token 是能热改的
    assert "token" in result["api_changed"]


def test_reload_settings_is_noop_when_config_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    cfg = _settings(tmp_path)
    rt = _make_runtime(monkeypatch, cfg)
    monkeypatch.setattr(
        runtime_module, "load_voice_agent_settings", lambda: (cfg, VoiceApiSettings())
    )
    result = asyncio.run(rt.reload_settings())
    assert result["changed"] == []
    assert result["notes"] == []


def test_reasoning_effort_hot_reload_applies_without_replacing_backend(monkeypatch, tmp_path):
    from operations.models import ReasoningEffort

    rt = _make_runtime(monkeypatch, _settings(tmp_path, backend="aliyun_deepseek"))
    original_backend = rt.agent.backend
    fresh = _settings(tmp_path, backend="aliyun_deepseek", deepseek_reasoning_effort=ReasoningEffort.HIGH)
    monkeypatch.setattr(runtime_module, "load_voice_agent_settings", lambda: (fresh, VoiceApiSettings()))
    result = asyncio.run(rt.reload_settings())
    assert not result["hot_reload_failed"]
    assert result["restart_keys"] == []
    assert rt.agent.backend is original_backend
    assert rt.agent.backend.settings.deepseek_reasoning_effort == ReasoningEffort.HIGH
    assert any("下一次" in note for note in result["notes"])


@pytest.mark.parametrize("updates", [
    {"first_audio_timeout_seconds": 60, "deepseek_reasoning_effort": "high"},
    {"audio_gap_timeout_seconds": 7, "output_volume": 70},
    {"first_audio_timeout_seconds": 60, "audio_gap_timeout_seconds": 7,
     "deepseek_reasoning_effort": "high", "output_volume": 70},
])
def test_combined_noninterrupting_reload_preserves_active_flows(monkeypatch, tmp_path, updates):
    async def run():
        runtime = _make_runtime(monkeypatch, _settings(tmp_path, backend="aliyun_deepseek"))
        original_backend = runtime.agent.backend
        release = asyncio.Event()
        announcement = asyncio.create_task(release.wait())
        control = asyncio.create_task(release.wait())
        runtime.agent._announcement_task = announcement
        runtime.agent._voice_control_task = control
        fresh = _settings(tmp_path, backend="aliyun_deepseek", **updates)
        monkeypatch.setattr(runtime_module, "load_voice_agent_settings", lambda: (fresh, VoiceApiSettings()))
        try:
            result = await runtime.reload_settings()
            assert not result["hot_reload_failed"]
            assert not announcement.done() and not control.done()
            assert runtime.agent.backend is original_backend
            for field, value in updates.items():
                assert getattr(runtime.agent.settings, field) == value
        finally:
            announcement.cancel()
            control.cancel()
            await asyncio.gather(announcement, control, return_exceptions=True)
            runtime.agent._announcement_task = runtime.agent._voice_control_task = None
            await runtime.agent.stop()
    asyncio.run(run())
