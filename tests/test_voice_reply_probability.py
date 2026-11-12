"""Probability controls whole automatic replies, never individual audio fragments."""

import asyncio
import random

import pytest
from support.live_runtime import audio_message, make_live_agent

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


@pytest.mark.parametrize("percent,draws,expected", [(0, [], [False, False]), (100, [], [True, True]), (30, [0.3, 0.299], [False, True])])
def test_cascade_policy_selects_whole_expression_after_recognition(percent, draws, expected):
    from behavior import create_behavior
    from behavior.models import AudioReady, RecognitionObserved, ReplySettings, Speak
    from channels.models import RoomTarget, SessionRef
    from context.models import ContextRevision

    samples = iter(draws)
    behavior = create_behavior(random_value=lambda: next(samples))
    session = SessionRef("run", "binding", "session", RoomTarget("a", "c"))
    behavior.start_session(session)
    selected = []
    for identifier in ("first", "second"):
        trigger = AudioReady(identifier, session, "u", ContextRevision())
        permit = behavior.admit(trigger, 0)
        result = behavior.decide(RecognitionObserved(trigger, permit, "heard human", ContextRevision()), ReplySettings(probability=percent))
        selected.append(isinstance(result, Speak))
    assert selected == expected
    assert behavior.snapshot().attempts == 2


@pytest.mark.parametrize("retry", [False, True])
def test_cancelled_explicit_send_does_not_force_the_next_automatic_reply(tmp_path, retry):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, reply_probability_percent=0, force_reply_keywords=[])
        socket = sockets[0]
        original = socket.send
        attempts = []
        async def cancel_text(text):
            import json
            if "text" in json.loads(text).get("realtimeInput", {}):
                attempts.append(True)
                if retry and len(attempts) == 1:
                    raise RuntimeError("external send failed")
                raise asyncio.CancelledError
            return await original(text)
        socket.send = cancel_text
        try:
            with pytest.raises(asyncio.CancelledError if not retry else RuntimeError):
                await agent.speak_text("cancelled command")
            await socket.deliver(audio_message(text="must not play", done=True))
            assert playback.chunks == []
        finally:
            await agent.leave()
    asyncio.run(run())


def test_live_zero_probability_drops_reply_but_keeps_listening_and_human_memory(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, reply_probability_percent=0, force_reply_keywords=[])
        try:
            await playback.capture("human", b"\0\x40" * 320)
            await sockets[0].deliver(audio_message(user_text="human", text="silent reply", done=True))
            assert [message for message in sockets[0].sent if "audio" in message.get("realtimeInput", {})]
            assert not playback.chunks and not agent.last_reply
            assert not agent.status()["speaking"]
            assert agent.turns == 0
            async def committed():
                while not agent.memory.as_messages():
                    await asyncio.sleep(0)
            await asyncio.wait_for(committed(), 1)
            assert agent.memory.as_messages() == [{"role": "user", "content": "human"}]
        finally:
            await agent.leave()
    asyncio.run(run())


def test_live_uses_one_probability_draw_per_turn_and_hot_changes_apply_next_turn(tmp_path, monkeypatch):
    samples = iter([0.8, 0.1])
    monkeypatch.setattr(random, "random", lambda: next(samples))
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, reply_probability_percent=30, force_reply_keywords=[])
        socket = sockets[0]
        try:
            await socket.deliver(audio_message(b"\1\0"))
            agent.settings.reply_probability_percent = 100
            await socket.deliver(audio_message(b"\2\0", done=True))
            assert playback.chunks == []
            await socket.deliver(audio_message(b"\3\0"))
            agent.settings.reply_probability_percent = 0
            await socket.deliver(audio_message(b"\4\0", text="reply", done=True))
            await agent.wait_for_reply_end(timeout=1)
            assert playback.chunks == [b"\3\0", b"\4\0"]
            agent.settings.reply_probability_percent = 30
            await socket.deliver(audio_message(b"\5\0", text="next", done=True))
            await agent.wait_for_reply_end(timeout=1)
            assert playback.chunks[-1] == b"\5\0"
            assert agent.turns == 2
        finally:
            await agent.leave()
    asyncio.run(run())


def test_live_explicit_speech_bypasses_zero_probability(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, reply_probability_percent=0, force_reply_keywords=[])
        try:
            assert (await agent.speak_text("hello"))["ok"]
            await sockets[0].deliver(audio_message(b"\1\0", text="hello", done=True))
            assert await agent.wait_for_reply_end(timeout=1)
            assert playback.chunks == [b"\1\0"]
            await sockets[0].deliver(audio_message(b"\2\0", done=True))
            assert playback.chunks == [b"\1\0"]
        finally:
            await agent.leave()
    asyncio.run(run())
