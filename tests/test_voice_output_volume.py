
"""Voice volume survives old configs, hot reloads and browser recreation."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from support.channel_fixtures import establish_room

from channels import create_duplex as VoiceDuplex
from oopz_sdk.transport.voice_browser import BrowserVoiceTransport
from operations import load_voice_agent_settings, patch_config_text
from operations.models import VoiceAgentSettings, VoiceApiSettings
from voice_agent.agent import VoiceAgent
from webui import config_editor as editor


@pytest.mark.parametrize("raw, expected", [
    ({}, 50), ({"output_volume": None}, 50), ({"output_volume": 0}, 10),
    ({"output_volume": "75"}, 75), ({"output_volume": 100}, 100),
    ({"output_volume": -1}, 10), ({"output_volume": 101}, 100),
    ({"output_volume": "bad"}, 50), ({"output_volume": float("nan")}, 50),
])
def test_load_volume_is_safe_for_existing_configs(monkeypatch, raw, expected):
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(VOICE_AGENT_CONFIG=raw))
    settings, _ = load_voice_agent_settings()
    assert settings.output_volume == expected


def test_old_config_displays_default_and_saving_inserts_volume(monkeypatch):
    monkeypatch.setattr(editor, "_runtime_config", lambda: SimpleNamespace(VOICE_AGENT_CONFIG={}))
    assert editor.schema_payload()["voice"]["fields"]["output_volume"]["value"] == 50
    updates = editor._normalize_updates({"voice": {"output_volume": 10}})
    source = patch_config_text('VOICE_AGENT_CONFIG = {"enabled": False}\n', updates, sources=editor.GROUP_SOURCES, filename=editor.CONFIG_PATH)
    assert '"output_volume": 10' in source


@pytest.mark.parametrize("value", [-1, 0, 9, 101, 50.5, True, "bad"])
def test_webui_rejects_invalid_volume(value):
    with pytest.raises(ValueError):
        editor._normalize_updates({"voice": {"output_volume": value}})


def test_duplex_passes_current_volume_with_every_pcm_chunk():
    async def run():
        transport = SimpleNamespace(push_tts_pcm=AsyncMock(return_value={"ok": True}))
        duplex = VoiceDuplex(transport)
        await duplex.push_tts_pcm(b"pcm", 24000)
        transport.push_tts_pcm.assert_awaited_with(b"pcm", 24000, finish=False, volume=50)
        duplex.output_muted = True
        await duplex.push_tts_pcm(b"", 24000, finish=True)
        transport.push_tts_pcm.assert_awaited_with(b"", 24000, finish=True, volume=0)
    asyncio.run(run())


def test_hot_volume_change_updates_browser_without_rebuilding_model(tmp_path):
    async def run():
        settings = VoiceAgentSettings(output_volume=75, memory_path=str(tmp_path / "memory.jsonl"))
        agent = VoiceAgent(settings, VoiceApiSettings())
        transport = SimpleNamespace(set_tts_volume=AsyncMock(return_value={"ok": True}))
        duplex = VoiceDuplex(transport)
        agent.bind_duplex(duplex)
        assert duplex.output_volume == 75
        await establish_room(agent)
        agent.output_controls.muted = True
        agent.backend.aclose = AsyncMock()
        agent._cancel_announcement = AsyncMock()
        agent._cancel_voice_control = AsyncMock()
        settings.output_volume = 10
        notes = await agent.refresh(["output_volume"])
        assert duplex.output_volume == 10
        assert not agent.output_muted and not duplex.output_muted
        transport.set_tts_volume.assert_awaited_once_with(10)
        agent.backend.aclose.assert_not_awaited()
        agent._cancel_announcement.assert_not_awaited()
        agent._cancel_voice_control.assert_not_awaited()
        assert any("音量" in note for note in notes)
    asyncio.run(run())


def test_idle_volume_change_does_not_start_browser(tmp_path):
    async def run():
        agent = VoiceAgent(VoiceAgentSettings(memory_path=str(tmp_path / "memory.jsonl")), VoiceApiSettings())
        transport = SimpleNamespace(set_tts_volume=AsyncMock())
        agent.bind_duplex(VoiceDuplex(transport))
        agent.settings.output_volume = 25
        await agent.refresh(["output_volume"])
        assert agent.duplex.output_volume == 25
        transport.set_tts_volume.assert_not_awaited()
    asyncio.run(run())


def test_browser_transport_forwards_volume_with_pcm():
    async def run():
        transport = BrowserVoiceTransport.__new__(BrowserVoiceTransport)
        transport._run_on_browser = AsyncMock(return_value={"ok": True})
        await transport.push_tts_pcm(b"\x00\x40", 24000, volume=25)
        transport._run_on_browser.assert_awaited_once_with("agoraPushTtsPcm", "AEA=", 24000, False, 25)
    asyncio.run(run())


def test_saved_volume_survives_config_reload(monkeypatch, tmp_path):
    config_path = tmp_path / "config.py"
    config_path.write_text('VOICE_AGENT_CONFIG = {"enabled": False}\n', encoding="utf-8")
    runtime_config = SimpleNamespace(VOICE_AGENT_CONFIG={"enabled": False})
    monkeypatch.setitem(sys.modules, "config", runtime_config)
    monkeypatch.setattr(editor, "CONFIG_PATH", str(config_path))
    result = editor.apply_updates({"voice": {"output_volume": 25}})
    assert not result["restart_required"]
    assert load_voice_agent_settings()[0].output_volume == 25
    namespace = {}
    exec(compile(config_path.read_text(encoding="utf-8"), str(config_path), "exec"), namespace)
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(**namespace))
    assert load_voice_agent_settings()[0].output_volume == 25
