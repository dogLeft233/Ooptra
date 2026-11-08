"""级联的当前问题只应发送一次，生成读取历史但不提前提交本轮草稿。"""

from __future__ import annotations

import asyncio

from support.dialogue_fixtures import mimo_backend as MimoCascadeBackend

from context import create_memory as MemoryStore
from operations.models import VoiceAgentSettings


def test_current_question_is_not_duplicated_in_chat_request(monkeypatch, tmp_path):
    captured = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self, **kwargs):
            return {"choices": [{"message": {"content": "answer"}}]}

    class Session:
        def post(self, url, **kwargs):
            captured.extend(kwargs["json"]["messages"])
            return Response()

    async def run():
        memory = MemoryStore(str(tmp_path / "memory.jsonl"))
        memory.append("user", "previous", user_key="oopz:u")
        memory.append("assistant", "previous reply", user_key="oopz:u")
        backend = MimoCascadeBackend(VoiceAgentSettings(
            mimo_api_key="test", reply_probability_percent=100, voice_leave_enabled=False,
            voice_volume_enabled=False,
        ), memory)

        async def asr(*args):
            return "current question"

        async def tts(*args):
            return b"", 24000

        async def http():
            return Session()

        monkeypatch.setattr(backend, "asr", asr)
        monkeypatch.setattr(backend, "tts", tts)
        monkeypatch.setattr(backend, "_http", http)
        result = await backend.handle_utterance(b"\x01\x00" * 320, sample_rate=16000, user_key="oopz:u")
        assert result.user_text == "current question"
        assert result.text == "answer"
        assert [m["content"] for m in captured if m["role"] == "user"] == [
            "previous", "current question",
        ]
        assert [m["content"] for m in memory.as_messages(user_key="oopz:u")] == [
            "previous", "previous reply",
        ]

    asyncio.run(run())
