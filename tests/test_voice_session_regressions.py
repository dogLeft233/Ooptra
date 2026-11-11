"""活跃 Live 会话更新、进房回滚和抢话后的输出抑制。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from support.channel_fixtures import establish_room
from test_gemini_session_lifecycle import FakeLiveWs

from operations.models import VoiceAgentSettings, VoiceApiSettings
from voice_agent.agent import VoiceAgent
from webui.voice_routes import mount_voice_routes


class RecordingDuplex:
    output_muted = False
    output_volume = 50

    def __init__(self, transport=None):
        self.transport = transport
        self.audio = []
        self.stops = 0

    async def enable_listen(self, enabled=True):
        return {"ok": True}

    async def push_tts_pcm(self, pcm, rate, *, finish=False):
        if pcm:
            self.audio.append(pcm)
        return {"ok": True}

    async def wait_tts_complete(self, timeout=30):
        return {"ok": True}

    async def stop_tts(self):
        self.stops += 1
        self.audio.clear()
        return {"ok": True}


@pytest.fixture
def opened(monkeypatch):
    from dialogue import create_gemini_backend
    from voice_agent import agent as agent_module

    sockets = []

    async def open_ws(*args, **kwargs):
        ws = FakeLiveWs()
        sockets.append(ws)
        return ws

    def create_backend(settings, memory, *, reply_policy):
        return create_gemini_backend(settings, memory, reply_policy=reply_policy, connector=open_ws)

    monkeypatch.setattr(agent_module, "create_backend", create_backend)
    return sockets


def make_agent(tmp_path, **kwargs):
    kwargs.setdefault("reply_probability_percent", 100)
    kwargs.setdefault("voice_leave_enabled", False)
    kwargs.setdefault("voice_volume_enabled", False)
    settings = VoiceAgentSettings(
        enabled=True, gemini_api_key="test", memory_path=str(tmp_path / "memory.jsonl"),
        **kwargs,
    )
    agent = VoiceAgent(settings, VoiceApiSettings())
    agent.duplex = RecordingDuplex()
    return agent


@pytest.mark.parametrize("field,value", [
    ("gemini_voice", "Kore"), ("gemini_model", "new-model"), ("persona", "new persona"),
    ("voice_leave_enabled", True),
])
def test_active_live_configuration_sends_fresh_setup(opened, tmp_path, field, value):
    async def run():
        agent = make_agent(tmp_path)
        await establish_room(agent)
        await establish_room(agent)
        await agent._wire_live()
        await agent.backend.start_session()
        setattr(agent.settings, field, value)
        try:
            await agent.refresh([field])
            assert len(opened) == 2, "活跃会话的配置变更没有建立新连接"
            assert opened[0].closed
            setup = opened[1].sent[0]["setup"]
            if field == "gemini_voice":
                assert setup["generation_config"]["speech_config"]["voice_config"][
                    "prebuilt_voice_config"
                ]["voice_name"] == "Kore"
            elif field == "gemini_model":
                assert setup["model"] == "models/new-model"
            elif field == "persona":
                assert setup["system_instruction"]["parts"][0]["text"].startswith("new persona")
            else:
                assert setup["tools"][0]["function_declarations"][0]["name"] == "leave_voice_room"
        finally:
            await agent.backend.close()

    asyncio.run(run())


def test_failed_live_refresh_does_not_claim_success(opened, tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await establish_room(agent)
        await agent.backend.start_session()
        agent.settings.gemini_api_key = ""
        try:
            notes = await agent.refresh(["gemini_api_key"])
            assert any("失败" in note for note in notes)
            assert not agent.backend.session_active
        finally:
            await agent.backend.close()

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_live_join_exits_room_and_cleans_state(monkeypatch, tmp_path, cancel):
    async def run():
        agent = make_agent(tmp_path)
        room = {"joined": False}
        transport = object()
        agent.duplex.transport = transport

        async def join(**kwargs):
            room["joined"] = True
            return SimpleNamespace(rtc_channel_name="test-room")

        async def leave():
            room["joined"] = False

        async def fail_session():
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("setup rejected")

        agent._bot = SimpleNamespace(voice=SimpleNamespace(
            backend=transport, join=join, leave=leave,
        ))
        monkeypatch.setattr(agent.backend, "start_session", fail_session)
        expected = asyncio.CancelledError if cancel else RuntimeError
        with pytest.raises(expected):
            await agent.join("area", "channel")
        assert not room["joined"], "模型失败后仍停留在语音房"
        assert not agent.status()["joined"]
        assert agent._round_task is None
        assert not agent.backend.session_active

    asyncio.run(run())


def test_live_barge_in_drops_remaining_audio_until_next_turn(monkeypatch, tmp_path):
    async def run():
        agent = make_agent(tmp_path, barge_in_hold_ms=20)
        await establish_room(agent)
        await agent._wire_live()

        async def discard_input(*args):
            pass

        monkeypatch.setattr(agent.backend, "push_audio", discard_input)
        await agent.backend._audio_out(b"first", 24000)
        assert agent.duplex.audio == [b"first"]
        await agent._on_remote_pcm("user", b"\x00\x40" * 320, 16000)
        await agent.backend._audio_out(b"rest", 24000)
        assert agent.duplex.audio == [], "被打断回合的后续音频再次进入播放队列"
        assert not agent.status()["speaking"]
        await agent.backend._handle_server_msg({"serverContent": {"turnComplete": True}})
        await agent.backend._audio_out(b"new turn", 24000)
        assert agent.duplex.audio == [b"new turn"]

    asyncio.run(run())


def test_cascade_silence_keeps_intentional_forced_interruption(tmp_path):
    async def run():
        agent = make_agent(tmp_path, backend="mimo_cascade")
        agent._speaking = True
        await agent._on_remote_pcm("silent user", bytes(640), 16000)
        assert agent.duplex.stops == 1
        assert not agent.status()["speaking"]
        assert agent._barged_in
        await agent.backend.aclose()

    asyncio.run(run())


def test_live_barge_in_clears_audio_whose_push_is_still_pending(monkeypatch, tmp_path):
    async def run():
        agent = make_agent(tmp_path, barge_in_hold_ms=20)
        await establish_room(agent)
        await agent._wire_live()
        entered, release, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def blocked_push(pcm, rate, *, finish=False):
            entered.set()
            await release.wait()
            agent.duplex.audio.append(pcm)
            return {"ok": True}

        async def interrupt():
            interrupted.set()

        async def discard_input(*args):
            pass

        monkeypatch.setattr(agent.duplex, "push_tts_pcm", blocked_push)
        monkeypatch.setattr(agent.backend, "interrupt", interrupt)
        monkeypatch.setattr(agent.backend, "push_audio", discard_input)
        push = asyncio.create_task(agent.backend._audio_out(b"old", 24000))
        await asyncio.wait_for(entered.wait(), 1)
        barge = asyncio.create_task(agent._on_remote_pcm("user", b"\x00\x40" * 320, 16000))
        try:
            await asyncio.wait_for(interrupted.wait(), 1)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(push, barge), 1)
        assert agent.duplex.audio == [], "清空队列后，在途分片仍然入队"
        assert not agent.status()["speaking"]

    asyncio.run(run())


def test_persona_endpoint_updates_the_active_live_session(opened, tmp_path, monkeypatch):
    import config
    from webui import config_editor
    path = tmp_path / "config.py"
    path.write_text('VOICE_AGENT_CONFIG = {"persona": "original"}\n', encoding="utf-8")
    monkeypatch.setattr(config_editor, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", {"persona": "original"})
    async def run():
        agent = make_agent(tmp_path)
        await establish_room(agent)
        await agent.backend.start_session()
        app = web.Application()
        mount_voice_routes(app, SimpleNamespace(agent=agent))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.put("/api/persona", json={"persona": "new persona"})
            assert response.status == 200
            assert len(opened) == 2
            setup = opened[1].sent[0]["setup"]
            assert setup["system_instruction"]["parts"][0]["text"].startswith("new persona")
        finally:
            await client.close()
            await agent.backend.close()

    asyncio.run(run())
