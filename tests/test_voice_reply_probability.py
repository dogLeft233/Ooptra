"""Probability controls whole automatic replies, never individual audio fragments."""

import asyncio
import base64
import random

import pytest
from support.channel_fixtures import establish_room
from test_gemini_session_lifecycle import FakeLiveWs
from test_round_throttle import _offer, _take
from test_voice_session_regressions import make_agent

from operations import load_voice_agent_settings
from webui import config_editor


def test_default_and_saved_probability_are_loaded(monkeypatch):
    import config

    for raw, expected in (({}, 30), ({"reply_probability_percent": 70}, 70)):
        monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", raw)
        settings, _ = load_voice_agent_settings()
        assert getattr(settings, "reply_probability_percent", None) == expected


def test_probability_is_a_basic_config_with_default_for_old_files(monkeypatch):
    import config

    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", {})
    fields = config_editor.schema_payload()["voice"]["fields"]
    field = fields.get("reply_probability_percent")
    assert field is not None
    assert (field["tier"], field["min"], field["max"], field["value"]) == ("basic", 0, 100, 30)
    assert config_editor._normalize_updates({"voice": {"reply_probability_percent": 30}}) == {
        "voice": {"reply_probability_percent": 30},
    }


@pytest.mark.parametrize("value", [-1, 101, "30.5"])
def test_probability_config_rejects_invalid_percentages(value):
    with pytest.raises(ValueError):
        config_editor._normalize_updates({"voice": {"reply_probability_percent": value}})


@pytest.mark.parametrize("percent,draws,expected", [
    (0, [], []), (100, [], [b"first", b"second"]),
    (30, [0.3, 0.299], [b"second"]),
])
def test_cascade_decides_before_asr(percent, draws, expected, monkeypatch, tmp_path):
    samples = iter(draws)
    monkeypatch.setattr(random, "random", lambda: next(samples))

    async def run():
        agent = make_agent(tmp_path, backend="mimo_cascade", reply_cooldown_ms=0)
        await establish_room(agent)
        agent.settings.reply_probability_percent = percent
        seen = []

        async def asr(pcm, rate):
            seen.append(pcm)
            return ""

        monkeypatch.setattr(agent.backend, "asr", asr)
        for pcm in (b"first", b"second"):
            _offer(agent, pcm)
            await agent._run_selected_reply(await _take(agent))
        assert seen == expected
        await agent.backend.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("retry", [False, True])
def test_cancelled_explicit_send_does_not_force_the_next_automatic_reply(tmp_path, monkeypatch, retry):
    from dialogue import create_gemini_backend
    from voice_agent import agent as agent_module

    async def open_ws(*args, **kwargs):
        return FakeLiveWs()

    def create_backend(settings, memory, *, reply_policy):
        return create_gemini_backend(settings, memory, reply_policy=reply_policy, connector=open_ws)

    monkeypatch.setattr(agent_module, "create_backend", create_backend)

    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        await establish_room(agent)
        agent._connection_ready = True
        await establish_room(agent)
        await agent._wire_live()
        await agent.backend.start_session()
        original_send = agent.backend._send
        attempts = []

        async def cancel_send(payload):
            if "realtimeInput" not in payload:
                return await original_send(payload)
            attempts.append(True)
            if retry and len(attempts) == 1:
                raise RuntimeError("send failed")
            raise asyncio.CancelledError

        monkeypatch.setattr(agent.backend, "_send", cancel_send)
        try:
            with pytest.raises(asyncio.CancelledError):
                await agent.speak_text("cancelled command")
            assert not agent.backend.reply_active
            await fragment(agent.backend, b"automatic", complete=True)
            assert not agent.duplex.audio
        finally:
            await agent.backend.aclose()

    asyncio.run(run())


async def fragment(backend, pcm=b"audio", *, text="", complete=False):
    server = {"modelTurn": {"parts": [{"inlineData": {
        "data": base64.b64encode(pcm).decode(), "mimeType": "audio/pcm;rate=24000",
    }}]}}
    if text:
        server.update(inputTranscription={"text": "human"}, outputTranscription={"text": text})
    if complete:
        server["turnComplete"] = True
    await backend._handle_server_msg({"serverContent": server})


def test_live_zero_probability_drops_reply_but_keeps_listening_and_human_memory(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        await establish_room(agent)
        await agent._wire_live()
        heard = []

        async def hear(pcm, rate):
            heard.append(pcm)

        monkeypatch.setattr(agent.backend, "push_audio", hear)
        await agent._on_remote_pcm("human", b"input", 16000)
        await fragment(agent.backend, text="silent reply", complete=True)
        assert heard == [b"input"]
        assert not agent.duplex.audio and not agent.last_reply
        assert not agent.status()["speaking"] and not agent._reply_generating
        assert agent.turns == 0
        assert [(r["role"], r["content"]) for r in agent.memory.recent()] == [("user", "human")]
        await agent.backend.aclose()

    asyncio.run(run())


def test_live_uses_one_probability_draw_per_turn_and_hot_changes_apply_next_turn(tmp_path, monkeypatch):
    samples = iter([0.8, 0.1])
    monkeypatch.setattr(random, "random", lambda: next(samples))

    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 30
        await establish_room(agent)
        await agent._wire_live()
        await fragment(agent.backend, b"skip-first")
        agent.settings.reply_probability_percent = 100
        await fragment(agent.backend, b"skip-second", complete=True)
        assert not agent.duplex.audio
        await fragment(agent.backend, b"allow-first")
        agent.settings.reply_probability_percent = 0
        await fragment(agent.backend, b"allow-second", complete=True)
        assert agent.duplex.audio == [b"allow-first", b"allow-second"]
        agent.settings.reply_probability_percent = 30
        await fragment(agent.backend, b"next", complete=True)
        assert agent.duplex.audio[-1] == b"next"
        assert agent.turns == 2
        await agent.backend.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["manual", "announcement"])
@pytest.mark.parametrize("retry", [False, True])
def test_live_explicit_speech_bypasses_zero_probability(tmp_path, monkeypatch, operation, retry):
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

    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        await establish_room(agent)
        agent._connection_ready = True
        await establish_room(agent)
        await agent._wire_live()
        await agent.backend.start_session()
        try:
            if retry:
                if operation == "manual":
                    sockets[0].dropped = True  # The explicit-send retry must keep its exemption.
                else:
                    await agent.backend.aclose()  # Announcement lazily opens a new session.
            if operation == "manual":
                assert (await agent.speak_text("hello"))["ok"]
            else:
                await agent.backend.speak_announcement("enter", "hello")
            await fragment(agent.backend, text="hello", complete=True)
            assert agent.duplex.audio == [b"audio"]
            await fragment(agent.backend, b"automatic", complete=True)
            assert agent.duplex.audio == [b"audio"]
        finally:
            await agent.backend.aclose()

    asyncio.run(run())
