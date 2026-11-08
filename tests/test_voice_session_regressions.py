"""真实应用验证活跃Live配置、进房回滚和抢话输出屏障。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from support.live_runtime import Playback, Socket, audio_message, make_live_agent, settle

from app.voice import VoiceCoordinator
from operations.models import VoiceAgentSettings, VoiceApiSettings
from webui.voice_routes import mount_voice_routes


class RecordingDuplex(Playback):
    def __init__(self, transport=None):
        super().__init__()
        self.transport = transport
        self.audio = self.chunks


@pytest.fixture
def opened(monkeypatch):
    from app import voice as module
    from dialogue import create_gemini_backend
    sockets = []
    async def connect(*args, **kwargs):
        socket = Socket()
        sockets.append(socket)
        return socket
    def factory(settings):
        return create_gemini_backend(settings, connector=connect)
    monkeypatch.setattr(module, "create_backend", factory)
    return sockets


def make_agent(tmp_path, **kwargs):
    defaults = dict(enabled=True, gemini_api_key="synthetic", reply_probability_percent=100,
                    voice_leave_enabled=False, voice_volume_enabled=False,
                    memory_path=str(tmp_path / "memory.jsonl"))
    defaults.update(kwargs)
    async def join(**kwargs):
        return SimpleNamespace(rtc_channel_name="rtc")
    async def leave():
        pass
    agent = VoiceCoordinator(VoiceAgentSettings(**defaults), VoiceApiSettings(),
                       bot=SimpleNamespace(voice=SimpleNamespace(backend=None, join=join, leave=leave)))
    agent.bind_duplex(RecordingDuplex())
    return agent


@pytest.mark.parametrize("field,value", [("gemini_voice", "Kore"), ("gemini_model", "new-model"),
    ("persona", "new persona"), ("voice_leave_enabled", True)])
def test_active_live_configuration_sends_fresh_setup(opened, tmp_path, field, value):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("area", "room")
        try:
            setattr(agent.settings, field, value)
            await agent.refresh([field])
            assert len(opened) == 2
            assert opened[0].closed
            setup = opened[1].sent[0]["setup"]
            if field == "gemini_voice":
                assert setup["generation_config"]["speech_config"]["voice_config"]["prebuilt_voice_config"]["voice_name"] == "Kore"
            elif field == "gemini_model":
                assert setup["model"] == "models/new-model"
            elif field == "persona":
                assert setup["system_instruction"]["parts"][0]["text"].startswith("new persona")
            else:
                assert any(tool["name"] == "leave_voice_room" for tool in setup["tools"][0]["function_declarations"])
        finally:
            await agent.leave()
    asyncio.run(run())


def test_failed_live_refresh_does_not_claim_success(opened, tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("area", "room")
        try:
            agent.settings.gemini_api_key = ""
            notes = await agent.refresh(["gemini_api_key"])
            assert any("失败" in note for note in notes)
            assert not agent.backend.connection_status()["ready"]
        finally:
            await agent.leave()
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_live_join_exits_room_and_cleans_state(monkeypatch, tmp_path, cancel):
    from app import voice as module
    from dialogue import create_gemini_backend
    async def run():
        room = {"joined": False}
        async def join(**kwargs):
            room["joined"] = True
            return SimpleNamespace(rtc_channel_name="rtc")
        async def leave():
            room["joined"] = False
        async def connect(*args, **kwargs):
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("setup rejected")
        monkeypatch.setattr(module, "create_backend", lambda settings: create_gemini_backend(settings, connector=connect))
        agent = make_agent(tmp_path)
        agent.bind_bot(SimpleNamespace(voice=SimpleNamespace(backend=None, join=join, leave=leave)))
        agent.bind_duplex(RecordingDuplex())
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await agent.join("area", "room")
        assert not room["joined"]
        assert not agent.status()["joined"]
        assert not agent.backend.connection_status()["ready"]
    asyncio.run(run())


def test_live_barge_in_drops_remaining_audio_until_next_turn(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, barge_in=True, barge_in_hold_ms=20)
        try:
            await sockets[0].deliver(audio_message(b"\1\0"))
            assert playback.chunks == [b"\1\0"]
            await playback.capture("user", b"\x00\x40" * 320)
            await sockets[0].deliver(audio_message(b"\2\0"))
            assert playback.chunks == []
            await sockets[0].deliver({"serverContent": {"turnComplete": True}})
            await sockets[0].deliver(audio_message(b"\3\0"))
            assert playback.chunks == [b"\3\0"]
        finally:
            await agent.leave()
    asyncio.run(run())


def test_live_barge_in_clears_audio_whose_push_is_still_pending(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, barge_in=True, barge_in_hold_ms=20)
        entered, released = asyncio.Event(), asyncio.Event()
        async def blocked_push(pcm, rate, *, finish=False):
            entered.set()
            try:
                await released.wait()
            except asyncio.CancelledError:
                # An external transport may finish accepting its in-flight request during cancellation.
                playback.chunks.append(pcm)
                raise
            playback.chunks.append(pcm)
            return {"ok": True}
        playback.push_tts_pcm = blocked_push
        try:
            await sockets[0].deliver(audio_message())
            await asyncio.wait_for(entered.wait(), 1)
            await playback.capture("user", b"\x00\x40" * 320)
            released.set()
            await settle()
            assert playback.chunks == []
            assert playback.stops == 1
            assert not agent.status()["speaking"]
        finally:
            released.set()
            await agent.leave()
    asyncio.run(run())


def test_persona_endpoint_updates_the_active_live_session(opened, tmp_path, monkeypatch):
    import config
    from webui import config_editor
    path = tmp_path / "config.py"
    path.write_text('VOICE_AGENT_CONFIG = {"persona": "original"}\n')
    monkeypatch.setattr(config_editor, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", {"persona": "original"})
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("area", "room")
        app = web.Application()
        mount_voice_routes(app, SimpleNamespace(agent=agent))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.put("/api/persona", json={"persona": "new persona"})
            assert response.status == 200
            assert len(opened) == 2
            assert opened[1].sent[0]["setup"]["system_instruction"]["parts"][0]["text"].startswith("new persona")
        finally:
            await client.close()
            await agent.leave()
    asyncio.run(run())
