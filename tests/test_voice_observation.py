"""Decision traces explain skipped speech without retaining room content."""

import asyncio
import json

import pytest
from support.channel_fixtures import establish_room
from test_voice_operations import make_agent
from test_voice_reply_probability import fragment


def test_observations_are_bounded_and_detached(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        for _ in range(45):
            await agent.backend.handle_utterance(b"audio", sample_rate=16000)
        payload = agent.status().get("voice_observation", {})
        rows = payload.get("recent_decisions", [])
        assert len(rows) == 40
        assert rows[-1]["reason"] == "probability"
        assert rows[-1]["outcome"] == "skipped"
        assert rows[-1]["timings_ms"]["asr"] is None
        rows.clear()
        assert len(agent.status()["voice_observation"]["recent_decisions"]) == 40
    asyncio.run(run())


def test_cascade_stage_timings_measure_real_request_boundaries(tmp_path, monkeypatch):
    from voice_agent import observation
    async def run():
        agent = make_agent(tmp_path)
        now = [100.0]
        monkeypatch.setattr(observation.time, "monotonic", lambda: now[0])
        async def asr(*args):
            now[0] += .12
            return "你好"
        async def chat(*args, **kwargs):
            now[0] += .25
            return "你好呀"
        async def tts(*args):
            now[0] += .4
            return b"\x01\x00", 24000
        agent.backend.asr, agent.backend.chat, agent.backend.tts = asr, chat, tts
        await agent.backend.handle_utterance(b"audio", sample_rate=16000)
        row = agent.status()["voice_observation"]["recent_decisions"][-1]
        assert row["timings_ms"] == {"asr": 120.0, "intent": None, "chat": 250.0, "tts": 400.0}
        assert row["elapsed_ms"] == 770.0
    asyncio.run(run())


def test_empty_live_turn_and_transport_error_are_observable(tmp_path, monkeypatch):
    from test_voice_session_regressions import make_agent as live_agent
    async def run():
        agent = live_agent(tmp_path)
        await establish_room(agent)
        await agent._wire_live()
        await agent.backend._handle_server_msg({"serverContent": {"turnComplete": True}})
        assert agent.status()["voice_observation"]["recent_decisions"][-1]["reason"] == "empty"
        async def send(*args):
            raise RuntimeError("secret")
        async def start():
            pass
        monkeypatch.setattr(agent.backend, "start_session", start)
        monkeypatch.setattr(agent.backend, "_send", send)
        await agent.backend.push_audio(b"\x00\x20" * 320, 16000)
        assert agent.status()["voice_observation"]["recent_decisions"][-1]["reason"] == "error"
        await agent.backend.aclose()
    asyncio.run(run())


def test_live_explicit_speech_does_not_open_a_keyword_conversation_window(tmp_path):
    from test_voice_session_regressions import make_agent as live_agent
    async def run():
        agent = live_agent(tmp_path)
        agent.settings.reply_probability_percent = 0
        agent.settings.force_reply_keywords = ["human"]
        agent.settings.conversation_window_enabled = True
        agent.settings.conversation_window_seconds = 30
        await establish_room(agent)
        await agent._wire_live()
        agent.backend.note_input_speaker("member")
        agent.backend._force_reply = True
        await fragment(agent.backend, b"explicit", text="manual", complete=True)
        agent.backend.note_input_speaker("member")
        await fragment(agent.backend, b"unrelated", complete=True)
        assert agent.duplex.audio == [b"explicit"]
        await agent.backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["asr", "tts"])
def test_cascade_errors_record_only_stage_and_safe_reason(tmp_path, failure):
    async def run():
        agent = make_agent(tmp_path)
        async def asr(*args):
            if failure == "asr":
                raise RuntimeError("secret-key-and-room-text")
            return "你好"
        async def chat(*args, **kwargs):
            return "你好呀"
        async def tts(*args):
            raise RuntimeError("secret-key-and-room-text")
        agent.backend.asr, agent.backend.chat, agent.backend.tts = asr, chat, tts
        with pytest.raises(RuntimeError):
            await agent.backend.handle_utterance(b"audio", sample_rate=16000)
        row = agent.status()["voice_observation"]["recent_decisions"][-1]
        assert row["reason"] == "error" and row["outcome"] == "error"
        assert row["timings_ms"][failure] >= 0
        assert "secret" not in json.dumps(row)
    asyncio.run(run())


@pytest.mark.parametrize("percent,keyword,reason", [(0, [], "probability"), (100, [], "reply"), (0, ["human"], "keyword")])
def test_live_trace_explains_whole_turn_and_does_not_invent_separate_stages(tmp_path, percent, keyword, reason):
    from test_voice_session_regressions import make_agent as live_agent
    async def run():
        agent = live_agent(tmp_path)
        agent.settings.reply_probability_percent = percent
        agent.settings.force_reply_keywords = keyword
        await establish_room(agent)
        await agent._wire_live()
        await fragment(agent.backend, b"pcm", text="reply", complete=True)
        row = agent.status()["voice_observation"]["recent_decisions"][-1]
        assert row["reason"] == reason
        assert row["timing_mode"] == "live_overlap"
        assert row["timings_ms"]["intent"] is None
        assert row["timings_ms"]["asr"] is None  # No microphone input was sent in this turn.
        await agent.backend.aclose()
    asyncio.run(run())
