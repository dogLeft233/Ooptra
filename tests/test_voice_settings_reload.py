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

from operations import apply_settings
from operations.models import VoiceAgentSettings, VoiceApiSettings
from voice_agent import runtime as runtime_module
from voice_agent.agent import VoiceAgent


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
    agent = VoiceAgent(settings, VoiceApiSettings())

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


def test_reload_settings_rebuilds_vad_when_tuning_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    rt = _make_runtime(monkeypatch, _settings(tmp_path))
    old_vad = rt.agent._vad

    fresh = _settings(tmp_path, silence_ms=1234, max_utterance_ms=9000)
    monkeypatch.setattr(
        runtime_module, "load_voice_agent_settings", lambda: (fresh, VoiceApiSettings())
    )
    result = asyncio.run(rt.reload_settings())

    assert "silence_ms" in result["changed"]
    assert rt.agent._vad is not old_vad, "断句参数变了却没重建 VAD"
    assert any("断句参数" in note for note in result["notes"])


def test_reload_settings_rebuilds_memory_and_rewires_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from aiohttp import web

    async def run():
        captured = []

        async def model(request):
            captured.append(await request.json())
            return web.json_response({"choices": [{"message": {"content": "hello"}}]})

        app = web.Application()
        app.router.add_post('/chat/completions', model)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, '127.0.0.1', 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        model_options = {"backend": "mimo_cascade", "mimo_api_key": "synthetic",
                         "mimo_base_url": f"http://127.0.0.1:{port}"}
        rt = _make_runtime(monkeypatch, _settings(tmp_path, **model_options))
        old_memory = rt.agent.memory
        old_memory.append("user", "old history", user_key="member")
        fresh = _settings(tmp_path, memory_path=str(tmp_path / "other.jsonl"), **model_options)
        monkeypatch.setattr(runtime_module, "load_voice_agent_settings",
                            lambda: (fresh, VoiceApiSettings()))
        try:
            result = await rt.reload_settings()
            assert "memory_path" in result["changed"]
            rt.agent.memory.append("user", "new history", user_key="member")
            assert await rt.agent.backend.chat("current", user_key="member") == "hello"
            assert [message["content"] for message in captured[0]["messages"]
                    if message["role"] == "user"] == ["new history", "current"]
            assert [row["content"] for row in old_memory.recent()] == ["old history"]
        finally:
            await rt.agent.backend.aclose()
            await runner.cleanup()

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
