"""通过供应商连接边界验证转写和服务器会话内容不进入日志。"""

import asyncio
import json

from behavior import create_reply_policy
from context import create_memory
from dialogue import create_gemini_backend
from operations.models import VoiceAgentSettings


class ScriptedWebSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()

    async def send(self, raw):
        if 'setup' in json.loads(raw):
            await self.incoming.put(json.dumps({'setupComplete': {}}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def close(self):
        await self.incoming.put(None)


def test_live_logs_lengths_without_transcript_reply_or_session_handle(caplog, tmp_path):
    async def run():
        socket = ScriptedWebSocket()

        async def connect(*args, **kwargs):
            return socket

        backend = create_gemini_backend(
            VoiceAgentSettings(gemini_api_key='synthetic', reply_probability_percent=100),
            create_memory(str(tmp_path / 'history.jsonl')), reply_policy=create_reply_policy(), connector=connect,
        )
        finished = asyncio.Event()
        backend.set_generated_out(lambda result: finished.set())
        try:
            with caplog.at_level('INFO'):
                await backend.start_session()
                await socket.incoming.put(json.dumps({
                    'sessionResumptionUpdate': {'newHandle': 'secret-session-handle'},
                }))
                await socket.incoming.put(json.dumps({'serverContent': {
                    'inputTranscription': {'text': 'private-user-transcript'},
                    'outputTranscription': {'text': 'private-model-draft'},
                    'turnComplete': True,
                }}))
                await asyncio.wait_for(finished.wait(), 1)
            assert 'event=dialogue.live.generated' in caplog.text
            assert 'input_chars=23' in caplog.text
            assert 'private-user-transcript' not in caplog.text
            assert 'private-model-draft' not in caplog.text
            assert 'secret-session-handle' not in caplog.text
        finally:
            await backend.aclose()

    asyncio.run(run())
