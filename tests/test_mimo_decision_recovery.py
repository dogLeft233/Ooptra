"""A malformed semantic response must not silence a validated ordinary turn."""

import asyncio
import json
import logging
from collections import deque

import pytest
from support.dialogue_fixtures import mimo_backend as MimoCascadeBackend

from behavior.models import VoiceControlDecision, VolumeCommand
from context import create_memory as MemoryStore
from operations.models import VoiceAgentSettings


def make_backend(tmp_path, monkeypatch, responses):
    backend = MimoCascadeBackend(
        VoiceAgentSettings(
            mimo_api_key="test", reply_probability_percent=100, min_utterance_ms=0,
            voice_leave_enabled=True, voice_volume_enabled=True,
        ),
        MemoryStore(str(tmp_path / "memory.jsonl")),
    )
    pending = deque(responses)
    requests, spoken = [], []

    class Response:
        def __init__(self, status, content):
            self.status, self.content = status, content

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self, **kwargs):
            if self.status >= 400:
                return {"error": {"message": self.content}}
            return {"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": self.content,
            }}]}

    class Session:
        def post(self, url, **kwargs):
            requests.append(kwargs["json"])
            assert pending, "unexpected extra model request"
            response = pending.popleft()
            if callable(response):
                response = response(kwargs["json"])
            return Response(*response)

    async def http():
        return Session()

    async def asr(*args):
        return "今天天气怎么样"

    async def tts(text):
        spoken.append(text)
        return b"\x01\x00" * 32, 24000

    monkeypatch.setattr(backend, "_http", http)
    monkeypatch.setattr(backend, "asr", asr)
    monkeypatch.setattr(backend, "tts", tts)
    return backend, requests, spoken


@pytest.mark.parametrize("percent", [0, 100])
def test_semantic_requests_use_json_mode_to_reach_speech_or_control(tmp_path, monkeypatch, percent):
    def respond(body):
        if body.get("response_format") != {"type": "json_object"}:
            return 200, "普通文本不能作为控制判定"
        if percent == 0:
            return 200, '{"leave": false, "volume": {"action": "mute"}}'
        return 200, '{"leave": false, "volume": null, "reply": "出门带把伞。"}'

    async def run():
        backend, requests, spoken = make_backend(tmp_path, monkeypatch, [respond, respond])
        backend.settings.reply_probability_percent = percent
        reply = await backend.handle_utterance(b"audio", sample_rate=16000)
        if percent == 0:
            assert reply.control == VoiceControlDecision(volume=VolumeCommand("mute"))
            assert not spoken and not reply.pcm16
        else:
            assert spoken == ["出门带把伞。"] and reply.pcm16
        assert len(requests) == 1

    asyncio.run(run())


@pytest.mark.parametrize("fence", ["```json\n{}\n```", "```\n{}\n```"])
@pytest.mark.parametrize("percent", [0, 100])
def test_whole_fenced_json_is_validated_before_speech_or_control(tmp_path, monkeypatch, fence, percent):
    content = ('{"leave": false, "volume": {"action": "restore"}}' if percent == 0
               else '{"leave": false, "volume": null, "reply": "出门带把伞。"}')

    async def run():
        backend, requests, spoken = make_backend(tmp_path, monkeypatch, [(200, fence.format(content))])
        backend.settings.reply_probability_percent = percent
        reply = await backend.handle_utterance(b"audio", sample_rate=16000)
        if percent == 0:
            assert reply.control is not None
            assert reply.control.volume is not None
            assert reply.control.volume.action == "restore"
            assert not spoken and not reply.pcm16
        else:
            assert spoken == ["出门带把伞。"] and reply.pcm16
        assert len(requests) == 1

    asyncio.run(run())


@pytest.mark.parametrize("failed_response", [
    (200, "这里应该是普通文本"),
    (200, '{"leave": false, "volume": null}'),
    (503, "upstream unavailable"),
])
def test_valid_independent_no_control_decision_recovers_a_fresh_reply(tmp_path, monkeypatch, failed_response):
    async def run():
        backend, requests, spoken = make_backend(tmp_path, monkeypatch, [
            failed_response,
            (200, '{"leave": false, "volume": null}'),
            (200, "出门带把伞。"),
        ])
        reply = await backend.handle_utterance(b"audio", sample_rate=16000, user_key="oopz:u")
        assert reply.text == "出门带把伞。" and reply.pcm16
        assert spoken == ["出门带把伞。"] and reply.control is None
        assert len(requests) == 3
        assert "response_format" not in requests[-1]
        assert [m["content"] for m in requests[-1]["messages"] if m["role"] == "user"] == [
            "今天天气怎么样",
        ]
        row = backend.observation.snapshot()["recent_decisions"][-1]
        assert (row["reason"], row["outcome"], row["timing_mode"]) == ("reply", "replied", "stages")

    asyncio.run(run())


@pytest.mark.parametrize("intent,expected", [
    ('{"leave": true, "volume": null}', VoiceControlDecision(leave=True)),
    ('{"leave": false, "volume": {"action": "mute"}}',
     VoiceControlDecision(volume=VolumeCommand("mute"))),
    ('{"leave": "false", "volume": null}', None),
    ('解释：{"leave": true, "volume": null}', None),
    ('{"leave": false, "volume": {"action": "set", "value": 0}}', None),
])
def test_recovery_never_chats_before_a_valid_no_control_decision(tmp_path, monkeypatch, intent, expected):
    async def run():
        backend, requests, spoken = make_backend(tmp_path, monkeypatch, [
            (200, "invalid combined response"), (200, intent),
        ])
        reply = await backend.handle_utterance(b"audio", sample_rate=16000)
        assert reply.control == expected and not reply.pcm16 and not spoken
        assert len(requests) == 2

    asyncio.run(run())


def test_combined_failure_logs_stage_without_upstream_secrets(tmp_path, monkeypatch, caplog):
    async def run():
        backend, _, _ = make_backend(tmp_path, monkeypatch, [
            (503, "secret-key-and-room-text"), (200, '{"leave": true, "volume": null}'),
        ])
        with caplog.at_level(logging.WARNING, logger="dialogue._internal.cascade.mimo_cascade"):
            await backend.handle_utterance(b"audio", sample_rate=16000)
        assert "合并请求失败" in caplog.text and "RuntimeError" in caplog.text
        assert "secret-key-and-room-text" not in caplog.text

    asyncio.run(run())


def test_failed_fresh_reply_keeps_upstream_body_out_of_agent_logs_and_status(tmp_path, monkeypatch, caplog):
    from support.channel_fixtures import establish_room
    from test_voice_operations import make_agent

    async def run():
        backend, requests, spoken = make_backend(tmp_path, monkeypatch, [
            (200, "invalid combined response"),
            (200, '{"leave": false, "volume": null}'),
            (503, "secret-key-and-room-text"),
        ])
        agent = make_agent(tmp_path)
        agent.backend = backend
        await establish_room(agent)
        with caplog.at_level(logging.WARNING):
            await agent._handle_utterance("u", b"audio", 16000)
        assert len(requests) == 3 and not spoken
        assert "LLM failed HTTP 503" in agent.last_reply
        assert "secret-key-and-room-text" not in agent.last_reply
        assert "secret-key-and-room-text" not in caplog.text
        row = backend.observation.snapshot()["recent_decisions"][-1]
        assert (row["reason"], row["outcome"], row["timing_mode"]) == ("error", "error", "stages")

    asyncio.run(run())


def test_recovery_timings_include_both_chat_requests(tmp_path, monkeypatch):
    import time

    async def run():
        now = [100.0]
        backend, _, _ = make_backend(tmp_path, monkeypatch, [])
        monkeypatch.setattr(time, "monotonic", lambda: now[0])

        async def chat(*args, _with_intent=False, **kwargs):
            now[0] += 2.0 if _with_intent else .5
            return "not JSON" if _with_intent else "出门带把伞。"

        async def intent(*args):
            now[0] += .25
            from behavior.models import VoiceControlDecision
            return VoiceControlDecision()

        monkeypatch.setattr(backend, "chat", chat)
        monkeypatch.setattr(backend, "detect_control_intent", intent)
        reply = await backend.handle_utterance(b"audio", sample_rate=16000)
        assert reply.pcm16
        row = backend.observation.snapshot()["recent_decisions"][-1]
        assert row["timing_mode"] == "stages"
        assert row["timings_ms"] == {"asr": 0.0, "intent": 250.0, "chat": 2500.0, "tts": 0.0}
        assert row["elapsed_ms"] == 2750.0
        assert "secret" not in json.dumps(row)

    asyncio.run(run())
