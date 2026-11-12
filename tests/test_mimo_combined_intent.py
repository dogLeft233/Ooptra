"""Selected normal turns use one semantic request; 0% still checks departure."""

import asyncio
import json

import pytest
from test_voice_operations import make_agent

from behavior.models import VoiceControlDecision


@pytest.mark.parametrize("leave,percent,expected_models", [(False, 100, ["chat", "tts"]), (True, 100, ["chat"]), (True, 0, ["intent"])])
def test_selected_turn_combines_intent_and_chat_but_zero_percent_can_leave(tmp_path, monkeypatch, leave, percent, expected_models):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = True
        agent.settings.reply_probability_percent = percent
        agent.settings.min_utterance_ms = 0
        agent.settings.mimo_api_key = "test"
        requests = []
        class Response:
            status = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def json(self, **kwargs):
                body = requests[-1]
                value = {"leave": leave} if body["max_tokens"] == 64 else {"leave": leave, "reply": "好的呀"}
                return {"choices": [{"message": {"content": json.dumps(value)}}]}
        class Session:
            def post(self, url, **kwargs):
                requests.append(kwargs["json"])
                return Response()
        async def http():
            return Session()
        async def asr(*args):
            return "你先离开这里吧" if leave else "今天天气不错"
        async def tts(text):
            assert text == "好的呀"
            requests.append({"model": "tts"})
            return b"\x01\x00" * 32, 24000
        monkeypatch.setattr(agent.backend, "_http", http)
        monkeypatch.setattr(agent.backend, "asr", asr)
        monkeypatch.setattr(agent.backend, "tts", tts)
        reply = await agent.backend.handle_utterance(b"audio", sample_rate=16000)
        kinds = ["tts" if item["model"] == "tts" else "intent" if item["max_tokens"] == 64 else "chat" for item in requests]
        assert kinds == expected_models
        assert (reply.control is not None and reply.control.leave) is leave
        assert bool(reply.pcm16) is (not leave)
        instructions = requests[0]["messages"][0]["content"]
        assert "不要出去" in instructions and "他说你出去吧" in instructions
        assert agent.status()["voice_observation"]["recent_decisions"][-1]["reason"] == ("control" if leave else "reply")
        await agent.backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("raw", ['{"leave": "true", "reply": "走啦"}', '{"reply": "你好"}', 'not JSON'])
def test_malformed_combined_result_never_triggers_leave_or_leaks_json_to_tts(tmp_path, monkeypatch, raw):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = True
        agent.settings.min_utterance_ms = 0
        agent.settings.mimo_api_key = "test"
        responses = iter([raw, "你好呀"])
        class Response:
            status = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def json(self, **kwargs):
                return {"choices": [{"message": {"content": next(responses)}}]}
        class Session:
            def post(self, *args, **kwargs):
                return Response()
        async def http():
            return Session()
        async def asr(*args):
            return "你好"
        async def intent(text):
            return VoiceControlDecision()
        async def tts(text):
            assert text == "你好呀", "only the fresh ordinary reply may be spoken"
            return b"\x01\x00" * 32, 24000
        monkeypatch.setattr(agent.backend, "_http", http)
        agent.backend.asr, agent.backend.detect_control_intent, agent.backend.tts = asr, intent, tts
        reply = await agent.backend.handle_utterance(b"audio", sample_rate=16000)
        assert reply.pcm16 and reply.control is None
        assert agent.status()["voice_observation"]["recent_decisions"][-1]["reason"] == "reply"
        await agent.backend.aclose()
    asyncio.run(run())


def test_failed_combined_request_still_checks_semantic_leave_and_never_speaks(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = True
        agent.settings.min_utterance_ms = 0
        async def asr(*args):
            return "你暂时不用在这里陪着了"
        async def combined(*args, **kwargs):
            raise RuntimeError("upstream unavailable")
        async def intent(text):
            assert text == "你暂时不用在这里陪着了"
            return VoiceControlDecision(leave=True)
        async def tts(*args):
            pytest.fail("control must leave farewell to the agent operation")
        agent.backend.asr, agent.backend.chat_with_intent = asr, combined
        agent.backend.detect_control_intent, agent.backend.tts = intent, tts
        reply = await agent.backend.handle_utterance(b"audio", sample_rate=16000)
        assert reply.control is not None and reply.control.leave
        assert not reply.pcm16
        assert agent.status()["voice_observation"]["recent_decisions"][-1]["reason"] == "control"
    asyncio.run(run())


def test_malformed_isolated_intent_is_observed_as_error_not_probability(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.voice_volume_enabled = False
        agent.settings.voice_leave_enabled = True
        agent.settings.reply_probability_percent = 0
        agent.settings.min_utterance_ms = 0
        agent.settings.mimo_api_key = "test"
        class Response:
            status = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def json(self, **kwargs):
                return {"choices": [{"message": {"content": '{"leave": "false"}'}}]}
        class Session:
            def post(self, *args, **kwargs):
                return Response()
        async def http():
            return Session()
        async def asr(*args):
            return "你好"
        monkeypatch.setattr(agent.backend, "_http", http)
        agent.backend.asr = asr
        reply = await agent.backend.handle_utterance(b"audio", sample_rate=16000)
        assert reply.control is None and not reply.pcm16
        assert agent.status()["voice_observation"]["recent_decisions"][-1]["reason"] == "error"
    asyncio.run(run())
