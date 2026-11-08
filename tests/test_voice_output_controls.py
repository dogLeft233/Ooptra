"""One output controller serves voice commands and authenticated UI routes."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from support.channel_fixtures import establish_room

from voice_agent.agent import VoiceAgent
from voice_agent.duplex import VoiceDuplex
from voice_agent.settings import VoiceAgentSettings, VoiceApiSettings
from webui.voice_routes import build_voice_routes


async def make_agent(tmp_path, volume=50):
    agent = VoiceAgent(VoiceAgentSettings(
        enabled=True, backend="mimo_cascade", output_volume=volume,
        memory_path=str(tmp_path / "memory.jsonl"),
    ), VoiceApiSettings())
    await establish_room(agent)
    agent.set_output_volume_saver(AsyncMock())
    transport = SimpleNamespace(set_tts_volume=AsyncMock(return_value={"ok": True}),
                                set_voice_state=AsyncMock(return_value=True),
                                push_tts_pcm=AsyncMock(return_value={"ok": True}))
    agent.bind_duplex(VoiceDuplex(transport))
    return agent, transport


@pytest.mark.parametrize("start, command, target", [
    (50, {"action": "increase"}, 60), (50, {"action": "decrease"}, 40),
    (95, {"action": "increase"}, 100), (15, {"action": "decrease"}, 10),
    (10, {"action": "decrease"}, 10), (50, {"action": "set", "value": 37}, 37),
])
def test_spoken_volume_is_bounded_saved_and_applied(tmp_path, start, command, target):
    async def run():
        agent, transport = await make_agent(tmp_path, start)
        result = await agent.apply_voice_volume(command)
        assert result["ok"] and result["volume"] == target and result["saved"]
        assert not result["muted"]
        agent.output_controls._saver.assert_awaited_once_with(target)
        transport.set_tts_volume.assert_awaited_once_with(target)
    asyncio.run(run())


def test_closed_mic_is_temporary_and_volume_change_opens_it(tmp_path):
    async def run():
        agent, transport = await make_agent(tmp_path, 70)
        result = await agent.apply_voice_volume({"action": "mute"})
        assert result["ok"] and result["muted"] and not result["saved"]
        assert agent.settings.output_volume == 70
        agent.output_controls._saver.assert_not_awaited()
        transport.set_tts_volume.assert_awaited_with(0)
        transport.set_voice_state.assert_awaited_with(mic_muted=True, speaker_muted=False)
        await agent.duplex.push_tts_pcm(b"pcm", 24000)
        transport.push_tts_pcm.assert_awaited_with(b"pcm", 24000, finish=False, volume=0)
        await agent.apply_voice_volume({"action": "decrease"})
        assert agent.settings.output_volume == 60 and not agent.output_muted
        transport.set_tts_volume.assert_awaited_with(60)
        transport.set_voice_state.assert_awaited_with(mic_muted=False, speaker_muted=False)
        await agent.apply_voice_volume({"action": "mute"})
        await agent.apply_voice_volume({"action": "restore"})
        assert not agent.output_muted and agent.settings.output_volume == 60
        transport.set_tts_volume.assert_awaited_with(60)
    asyncio.run(run())


@pytest.mark.parametrize("volume,command", [(10, {"action": "decrease"}),
                                          (100, {"action": "increase"}),
                                          (50, {"action": "set", "value": 50})])
def test_volume_commands_open_mic_even_when_volume_is_unchanged(tmp_path, volume, command):
    async def run():
        agent, transport = await make_agent(tmp_path, volume)
        await agent.set_output_controls(muted=True)
        result = await agent.apply_voice_volume(command)
        assert result["ok"] and not result["muted"] and result["volume"] == volume
        transport.set_tts_volume.assert_awaited_with(volume)
    asyncio.run(run())


def test_control_switch_does_not_disable_ui_and_rejects_spoken_commands(tmp_path):
    async def run():
        agent, _ = await make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        assert not (await agent.apply_voice_volume({"action": "mute"}))["ok"]
        assert (await agent.set_output_controls(muted=True))["ok"]
        assert (await agent.set_output_controls(volume=25))["volume"] == 25
    asyncio.run(run())


def test_failed_save_keeps_previous_volume_and_reports_failure(tmp_path):
    async def run():
        agent, transport = await make_agent(tmp_path)
        agent.output_controls._saver.side_effect = OSError("disk unavailable")
        result = await agent.apply_voice_volume({"action": "set", "value": 25})
        assert not result["ok"] and "disk unavailable" in result["error"]
        assert agent.settings.output_volume == 50
        transport.set_tts_volume.assert_not_awaited()
    asyncio.run(run())


def test_concurrent_increment_commands_are_serialized(tmp_path):
    async def run():
        agent, _ = await make_agent(tmp_path)
        await asyncio.gather(*(agent.apply_voice_volume({"action": "increase"}) for _ in range(3)))
        assert agent.settings.output_volume == 80
    asyncio.run(run())


def test_browser_failure_does_not_claim_mic_was_closed(tmp_path):
    async def run():
        agent, transport = await make_agent(tmp_path)
        transport.set_tts_volume.return_value = {"ok": False, "error": "browser disconnected"}
        result = await agent.set_output_controls(muted=True)
        assert not result["ok"] and not result["muted"]
        assert not agent.output_muted and not agent.duplex.output_muted
        agent.output_controls._saver.assert_not_awaited()
    asyncio.run(run())


def test_broadcast_failure_keeps_the_actual_audio_state(tmp_path, caplog):
    async def run():
        agent, transport = await make_agent(tmp_path)
        transport.set_voice_state.return_value = False
        result = await agent.set_output_controls(muted=True)
        assert result["ok"] and result["muted"]
        transport.set_tts_volume.assert_awaited_with(0)
        assert "状态广播失败" in caplog.text
    asyncio.run(run())


def test_old_room_failure_does_not_restore_mute_in_new_room(tmp_path):
    async def run():
        agent, transport = await make_agent(tmp_path)
        await agent.set_output_controls(muted=True)
        started, release = asyncio.Event(), asyncio.Event()
        async def failure(volume):
            started.set()
            await release.wait()
            return {"ok": False, "error": "old browser closed"}
        transport.set_tts_volume.side_effect = failure
        task = asyncio.create_task(agent.set_output_controls(muted=False))
        await started.wait()
        agent.output_controls.reset_mute()
        release.set()
        result = await task
        assert not result["ok"] and not agent.output_muted and not agent.duplex.output_muted
    asyncio.run(run())


def test_cancelled_voice_command_settles_save_before_releasing_lock(tmp_path):
    async def run():
        agent, _ = await make_agent(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        persisted = []
        async def save(volume):
            started.set()
            await release.wait()
            persisted.append(volume)
        agent.set_output_volume_saver(save)
        task = asyncio.create_task(agent.apply_voice_volume({"action": "set", "value": 40}))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert persisted == [40] and agent.settings.output_volume == 40
        assert agent.duplex.output_volume == 40
    asyncio.run(run())


def test_config_reload_cannot_be_overwritten_by_an_older_spoken_save(monkeypatch, tmp_path):
    import config
    from voice_agent.runtime import VoiceRuntime
    from webui import config_editor

    cfg = {"enabled": False, "backend": "mimo_cascade", "memory_path": str(tmp_path / "memory.jsonl")}
    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", cfg)
    path = tmp_path / "config.py"
    path.write_text(f"VOICE_AGENT_CONFIG = {cfg!r}\n", encoding="utf-8")
    monkeypatch.setattr(config_editor, "CONFIG_PATH", str(path))

    async def run():
        runtime = VoiceRuntime(state_path=tmp_path / "visits.json")
        saved, release = asyncio.Event(), asyncio.Event()
        async def save(volume):
            await runtime._save_output_volume(volume)
            saved.set()
            await release.wait()
        runtime.agent.set_output_volume_saver(save)
        task = asyncio.create_task(runtime.agent.set_output_controls(volume=60))
        await saved.wait()
        config_editor.apply_updates({"voice": {"output_volume": 80}})
        reload = asyncio.create_task(runtime.reload_settings())
        await asyncio.sleep(0)
        assert not reload.done()
        release.set()
        await task
        await reload
        assert runtime.agent.settings.output_volume == 80
        assert config.VOICE_AGENT_CONFIG["output_volume"] == 80
    asyncio.run(run())


def test_runtime_saves_spoken_volume_to_disk_but_never_temporary_mic(monkeypatch, tmp_path):
    import config
    from voice_agent.runtime import VoiceRuntime
    from webui import config_editor

    cfg = {"enabled": True, "backend": "mimo_cascade", "memory_path": str(tmp_path / "memory.jsonl")}
    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", cfg)
    path = tmp_path / "config.py"
    path.write_text(f"VOICE_AGENT_CONFIG = {cfg!r}\n", encoding="utf-8")
    monkeypatch.setattr(config_editor, "CONFIG_PATH", str(path))

    async def run():
        runtime = VoiceRuntime(state_path=tmp_path / "visits.json")
        await establish_room(runtime.agent)
        transport = SimpleNamespace(set_tts_volume=AsyncMock(return_value={"ok": True}))
        runtime.agent.bind_duplex(VoiceDuplex(transport))
        assert (await runtime.agent.apply_voice_volume({"action": "set", "value": 37}))["saved"]
        assert (await runtime.agent.apply_voice_volume({"action": "mute"}))["muted"]
        saved = path.read_text(encoding="utf-8")
        assert "37" in saved and "muted" not in saved
        namespace = {}
        exec(compile(saved, str(path), "exec"), namespace)
        monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", namespace["VOICE_AGENT_CONFIG"])
        restarted = VoiceRuntime(state_path=tmp_path / "visits2.json")
        assert restarted.agent.settings.output_volume == 37
        assert not restarted.agent.output_muted
    asyncio.run(run())


@pytest.mark.parametrize("body", [{"volume": 0}, {"volume": 9}, {"volume": 101},
                                       {"volume": True}, {"volume": "50"}, {"muted": "false"},
                                       {}, {"muted": True, "unknown": 1}])
def test_output_route_rejects_invalid_controls(tmp_path, body):
    async def run():
        agent, transport = await make_agent(tmp_path)
        route = next(r for r in build_voice_routes(SimpleNamespace(agent=agent))
                     if r.method == "POST" and r.path == "/voice/output")
        response = await route.handler(SimpleNamespace(can_read_body=True, json=AsyncMock(return_value=body)))
        assert response.status == 400
        transport.set_tts_volume.assert_not_awaited()
    asyncio.run(run())


def test_ui_volume_persists_but_mic_button_does_not(tmp_path):
    async def run():
        agent, _ = await make_agent(tmp_path)
        route = next(r for r in build_voice_routes(SimpleNamespace(agent=agent))
                     if r.method == "POST" and r.path == "/voice/output")
        response = await route.handler(SimpleNamespace(can_read_body=True, json=AsyncMock(return_value={"volume": 35})))
        assert response.status == 200 and json.loads(response.text)["volume"] == 35
        agent.output_controls._saver.assert_awaited_once_with(35)
        response = await route.handler(SimpleNamespace(can_read_body=True, json=AsyncMock(return_value={"muted": True})))
        assert response.status == 200 and json.loads(response.text)["muted"]
        assert agent.output_controls._saver.await_count == 1
    asyncio.run(run())


def test_rejoining_resets_temporary_mic_state(tmp_path):
    from test_voice_operations import make_agent as room_agent

    async def run():
        agent = room_agent(tmp_path)
        agent.set_output_volume_saver(AsyncMock())
        agent.duplex.set_output_volume = AsyncMock(return_value={"ok": True})
        await agent.join("area", "room")
        await agent.set_output_controls(muted=True)
        assert agent.status()["output_muted"]
        await agent.leave()
        await agent.join("area", "room")
        assert not agent.status()["output_muted"]
        assert agent.settings.output_volume == 50
        await agent.leave()
    asyncio.run(run())
