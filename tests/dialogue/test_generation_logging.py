"""通过供应商连接边界验证转写和服务器会话内容不进入日志。"""

import asyncio
import json

from channels.models import RoomTarget, SessionRef
from context.models import ContextRef, ContextRevision, PreparedContext
from dialogue import create_gemini_backend
from dialogue.models import LiveSessionRequest
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
            VoiceAgentSettings(gemini_api_key='synthetic', reply_probability_percent=100), connector=connect,
        )
        session = SessionRef("run", "binding", "room", RoomTarget("area", "channel"))
        prepared = PreparedContext(ContextRef(session, 0, "local"), ContextRevision(0), "", ())
        try:
            with caplog.at_level('INFO'):
                live = await backend.open_live(LiveSessionRequest(session, prepared))
                events = live.events()
                await socket.incoming.put(json.dumps({
                    'sessionResumptionUpdate': {'newHandle': 'secret-session-handle'},
                }))
                await socket.incoming.put(json.dumps({'serverContent': {
                    'inputTranscription': {'text': 'private-user-transcript'},
                    'outputTranscription': {'text': 'private-model-draft'},
                    'turnComplete': True,
                }}))
                for _ in range(3):
                    event = await asyncio.wait_for(anext(events), 1)
                    if event.kind == "updated" and event.text == "private-user-transcript":
                        break
                await events.aclose()
            assert 'event=dialogue.live.generated' in caplog.text
            assert 'input_chars=23' in caplog.text
            assert 'private-user-transcript' not in caplog.text
            assert 'private-model-draft' not in caplog.text
            assert 'secret-session-handle' not in caplog.text
        finally:
            await backend.aclose()

    asyncio.run(run())



def test_generation_finished_log_matches_job_after_supplier_close_failure(caplog, monkeypatch):
    import time

    import pytest

    from behavior.models import SpeechIntent
    from channels.models import OutputSourceError
    from dialogue import audio_output, create_mimo_backend

    async def run():
        backend = create_mimo_backend(VoiceAgentSettings())
        cls = type(backend)
        close = cls.aclose
        async def chat(self, *args, **kwargs):
            return "private assistant draft"
        async def tts(self, text):
            return b"\x01\x00", 24000
        async def failed_close(self):
            await close(self)
            raise RuntimeError("private provider cleanup detail")
        with monkeypatch.context() as patch:
            patch.setattr(cls, "chat", chat)
            patch.setattr(cls, "tts", tts)
            patch.setattr(cls, "aclose", failed_close)
            ref = SessionRef("run", "binding", "room", RoomTarget("area", "channel"))
            prepared = PreparedContext(ContextRef(ref, 0, "local"), ContextRevision(4), "", ())
            intent = SpeechIntent("intent", "action", ref, "input", prepared.version,
                                  time.monotonic() + 5, "private user text", "member")
            with caplog.at_level("INFO"):
                job = backend.generate(intent, prepared)
                output = audio_output(job)
                with pytest.raises(OutputSourceError):
                    _ = [frame async for frame in output.frames()]
            assert job.snapshot().status == "failed"
            assert job.snapshot().reason == "provider_failed"
            logs = [record.getMessage() for record in caplog.records if "event=voice.generation.finished" in record.getMessage()]
            assert len(logs) == 1
            assert "status=failed" in logs[0] and "reason=provider_failed" in logs[0]
            assert "input_ref=input" in logs[0] and "action_id=action" in logs[0]
            assert "private" not in caplog.text
        await backend.aclose()
    asyncio.run(run())
