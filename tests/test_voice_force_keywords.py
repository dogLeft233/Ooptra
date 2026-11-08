"""Keyword overrides survive imperfect casing and late Live transcription."""

import asyncio
import random

import pytest
from support.channel_fixtures import establish_room
from test_round_throttle import _offer, _take
from test_voice_reply_probability import fragment
from test_voice_session_regressions import make_agent

from voice_agent.settings import load_voice_agent_settings
from webui import config_editor


def test_force_keywords_load_and_are_editable_in_basic_config(monkeypatch):
    import config

    monkeypatch.setattr(config, "VOICE_AGENT_CONFIG", {"force_reply_keywords": [" Ooptra ", "机器人", ""]})
    settings, _ = load_voice_agent_settings()
    assert getattr(settings, "force_reply_keywords", None) == ["Ooptra", "机器人"]
    assert config_editor.schema_payload()["voice"]["fields"].get("force_reply_keywords", {}).get("tier") == "basic"
    assert config_editor._normalize_updates({"voice": {"force_reply_keywords": "Ooptra, 机器人"}}) == {
        "voice": {"force_reply_keywords": ["Ooptra", "机器人"]},
    }


@pytest.mark.parametrize("text,keywords,forced", [
    ("请问 oOpTrA 怎么办", ["OOPTRA"], True),
    ("Ｏｏｐｔｒａ，请帮忙", ["ooptra"], True),  # noqa: RUF001 - intentional full-width text
    ("Oop tra，你好", ["ooptra"], True),
    ("OOP-TRA，你好", ["ooptra"], True),
    ("机器 人，帮一下", ["机器人"], True),
    ("普通聊天", ["Ooptra", "机器人"], False),
    ("普通聊天", [" ", "..."], False),
])
def test_mimo_keyword_overrides_zero_probability_after_asr(tmp_path, monkeypatch, text, keywords, forced):
    async def run():
        agent = make_agent(tmp_path, backend="mimo_cascade", reply_cooldown_ms=0)
        await establish_room(agent)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = keywords
        calls = []

        async def asr(*args):
            calls.append("asr")
            return text

        async def chat(*args, **kwargs):
            calls.append("chat")
            return "answer"

        async def tts(*args):
            calls.append("tts")
            return b"\x01\x00" * 32, 24000

        monkeypatch.setattr(agent.backend, "asr", asr)
        monkeypatch.setattr(agent.backend, "chat", chat)
        monkeypatch.setattr(agent.backend, "tts", tts)
        monkeypatch.setattr(random, "random", lambda: pytest.fail("zero probability or keyword hit needs no draw"))
        _offer(agent, b"human speech")
        await agent._throttled_round(_take(agent))
        assert calls == (["asr", "chat", "tts"] if forced else ["asr"])
        assert bool(agent.duplex.audio) is forced
        assert agent.last_user_text == text
        assert [r["role"] for r in agent.memory.recent()] == (["user", "assistant"] if forced else ["user"])
        await agent.backend.aclose()

    asyncio.run(run())


def test_live_keyword_after_turn_complete_restores_only_the_original_reply(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = ["ooptra"]
        await establish_room(agent)
        await agent._wire_live()
        await fragment(agent.backend, b"original", complete=True)
        await agent.backend._handle_server_msg({"serverContent": {"inputTranscription": {"text": "Ooptra"}}})
        assert agent.duplex.audio == [b"original"]
        assert not agent._reply_generating and not agent.status()["speaking"]
        await fragment(agent.backend, b"unrelated", text="unrelated reply", complete=True)
        assert agent.duplex.audio == [b"original"]
        assert agent.turns == 1
        await agent.backend.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["voice", "expiry", "reset", "next_model"])
def test_live_ambiguous_late_keyword_cannot_force_a_new_reply(tmp_path, monkeypatch, boundary):
    from voice_agent.backends import gemini_live

    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = ["ooptra"]
        await establish_room(agent)
        await agent._wire_live()
        await fragment(agent.backend, b"old", complete=True)
        if boundary == "voice":
            async def noop(*args):
                pass
            monkeypatch.setattr(agent.backend, "start_session", noop)
            monkeypatch.setattr(agent.backend, "_send", noop)
            await agent.backend.push_audio(b"\x00\x20" * 320)
        elif boundary == "expiry":
            monkeypatch.setattr(gemini_live, "_LATE_TRANSCRIPTION_SECONDS", 0)
            # Advance the clock past the already established deadline.
            now = gemini_live.time.monotonic()
            monkeypatch.setattr(gemini_live.time, "monotonic", lambda: now + 10)
        elif boundary == "reset":
            await agent.backend._reset_session()
        else:
            await fragment(agent.backend, b"new-start")
        await agent.backend._handle_server_msg({"serverContent": {"inputTranscription": {"text": "Ooptra"}}})
        await fragment(agent.backend, b"new-end", complete=True)
        assert not agent.duplex.audio
        assert not agent._reply_generating
        await agent.backend.aclose()

    asyncio.run(run())


def test_live_silent_pcm_keeps_completed_reply_available(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = ["ooptra"]
        await establish_room(agent)
        await agent._wire_live()
        await fragment(agent.backend, b"old", complete=True)
        async def noop(*args):
            pass
        monkeypatch.setattr(agent.backend, "start_session", noop)
        monkeypatch.setattr(agent.backend, "_send", noop)
        await agent.backend.push_audio(b"\x00\x00" * 320)
        await agent.backend._handle_server_msg({"serverContent": {"inputTranscription": {"text": "Ooptra"}}})
        assert agent.duplex.audio == [b"old"]
        await agent.backend.aclose()

    asyncio.run(run())


def test_live_reset_during_late_audio_restore_discards_remaining_audio_and_memory(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = ["ooptra"]
        await establish_room(agent)
        await agent._wire_live()
        await fragment(agent.backend, b"first")
        await fragment(agent.backend, b"second", text="old answer", complete=True)
        delivered = []
        async def reset_on_audio(pcm, rate):
            delivered.append(pcm)
            await agent.backend._reset_session()
        agent.backend.set_audio_out(reset_on_audio)
        await agent.backend._handle_server_msg({"serverContent": {"inputTranscription": {"text": "Ooptra"}}})
        assert delivered == [b"first"]
        assert all(row["role"] != "assistant" for row in agent.memory.recent())
        assert not agent.backend.reply_active
        await agent.backend.aclose()

    asyncio.run(run())


def test_live_late_keyword_replays_the_whole_buffered_reply(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = ["ooptra"]
        await establish_room(agent)
        await agent._wire_live()
        await fragment(agent.backend, b"first")
        await agent.backend._handle_server_msg({"serverContent": {"inputTranscription": {"text": "OOP-"}}})
        await fragment(agent.backend, b"second")
        assert not agent.duplex.audio
        await agent.backend._handle_server_msg({"serverContent": {"inputTranscription": {"text": "ＴＲＡ，帮忙"}}})  # noqa: RUF001
        await fragment(agent.backend, b"last", complete=True)
        assert agent.duplex.audio == [b"first", b"second", b"last"]
        assert agent.turns == 1
        await fragment(agent.backend, b"unrelated", text="ordinary reply", complete=True)
        assert agent.duplex.audio == [b"first", b"second", b"last"]
        await agent.backend.aclose()

    asyncio.run(run())


def test_live_deferred_audio_budget_discards_whole_reply_and_resets_next_turn(tmp_path, monkeypatch):
    from voice_agent.backends import gemini_live

    monkeypatch.setattr(gemini_live, "_MAX_DEFERRED_AUDIO_BYTES", 4)

    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = ["human"]
        await establish_room(agent)
        await agent._wire_live()
        await fragment(agent.backend, b"too much")
        await fragment(agent.backend, b"tail", text="answer", complete=True)
        assert not agent.duplex.audio, "a late keyword must not play a truncated reply"
        await fragment(agent.backend, b"ok", text="answer", complete=True)
        assert agent.duplex.audio == [b"ok"]
        await agent.backend.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("sample,expected", [(0.2, True), (0.8, False)])
def test_mimo_nonmatching_keyword_draws_once_after_asr(tmp_path, monkeypatch, sample, expected):
    draws = []

    def draw():
        draws.append(sample)
        return sample

    monkeypatch.setattr(random, "random", draw)

    async def run():
        agent = make_agent(tmp_path, backend="mimo_cascade", reply_cooldown_ms=0)
        await establish_room(agent)
        agent.settings.reply_probability_percent = 30
        agent.settings.force_reply_keywords = ["nickname"]
        chatted = []

        async def asr(*args):
            return "ordinary question"

        async def chat(*args, **kwargs):
            chatted.append(True)
            return ""

        monkeypatch.setattr(agent.backend, "asr", asr)
        monkeypatch.setattr(agent.backend, "chat", chat)
        _offer(agent, b"speech")
        await agent._throttled_round(_take(agent))
        assert draws == [sample]
        assert bool(chatted) is expected
        await agent.backend.aclose()

    asyncio.run(run())


def test_live_keyword_edit_takes_effect_next_turn_without_partial_playback(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = []
        await establish_room(agent)
        await agent._wire_live()
        await fragment(agent.backend, b"already discarded")
        agent.settings.force_reply_keywords = ["human"]
        await fragment(agent.backend, b"also discarded", text="reply", complete=True)
        assert not agent.duplex.audio
        await fragment(agent.backend, b"next whole reply", text="answer", complete=True)
        assert agent.duplex.audio == [b"next whole reply"]
        await agent.backend.aclose()

    asyncio.run(run())
