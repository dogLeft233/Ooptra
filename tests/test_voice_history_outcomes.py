"""真实应用按渠道播放事实提交事务历史；生成草稿不能冒充已输出。"""
import asyncio
import base64

from aiohttp import web
from aiohttp.test_utils import TestServer
from support.channel_fixtures import make_voice_agent
from support.live_runtime import Playback, audio_message, make_live_agent, settle


def test_client_interruption_never_records_the_full_generated_reply(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, barge_in=True, barge_in_hold_ms=0)
        try:
            await sockets[0].deliver(audio_message(user_text="question", text="complete generated answer"))
            assert playback.chunks
            await playback.capture("u", b"\x00\x40" * 320)
            assert playback.stops == 1
            await sockets[0].deliver({"serverContent": {"turnComplete": True}})
            await agent.wait_for_reply_end(timeout=1)
            assert agent.memory.as_messages() == [{"role": "user", "content": "question"}]
        finally:
            await agent.leave()
    asyncio.run(run())


def test_same_room_context_refresh_rejects_late_cascade_result(tmp_path):
    async def run():
        entered, released, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        async def model(request):
            body = await request.json()
            calls.append(body["model"])
            if body["model"] == "asr":
                entered.set()
                try:
                    await released.wait()
                except asyncio.CancelledError:
                    await released.wait()
                finished.set()
                return web.json_response({"choices": [{"message": {"content": "old source question"}}]})
            if body.get("stream"):
                encoded = base64.b64encode(b"\0\x40" * 100).decode()
                return web.Response(text='data: {"choices":[{"delta":{"audio":{"data":"' + encoded + '"}}}]}\n\n' + 'data: [DONE]\n\n', content_type="text/event-stream")
            return web.json_response({"choices": [{"message": {"content": "stale reply"}}]})
        app = web.Application()
        app.router.add_post("/chat/completions", model)
        server = TestServer(app)
        await server.start_server()
        agent = make_voice_agent(tmp_path)
        playback = Playback()
        agent.settings.mimo_api_key = "synthetic"
        agent.settings.mimo_base_url = str(server.make_url("/")).rstrip("/")
        agent.settings.mimo_asr_model = "asr"
        agent.settings.voice_leave_enabled = False
        agent.settings.voice_volume_enabled = False
        agent.bind_duplex(playback)
        try:
            await agent.join("area", "room")
            original_session = agent.channels.snapshot().session
            # Real channel capture -> energy VAD -> application input task -> local HTTP ASR.
            for _ in range(40):
                await playback.capture("u", b"\0\x40" * 480)
            for _ in range(45):
                await playback.capture("u", bytes(960))
            await asyncio.wait_for(entered.wait(), 1)
            await agent.refresh_astrbot_context()
            released.set()
            await asyncio.wait_for(finished.wait(), 1)
            await settle()
            assert agent.channels.snapshot().session == original_session
            assert playback.chunks == []
            assert agent.memory.as_messages() == []
            assert calls == ["asr"], "stale ASR must never start expression generation"
        finally:
            released.set()
            await agent.leave()
            await server.close()
    asyncio.run(run())


def test_tail_playback_interruption_does_not_suppress_the_next_live_reply(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path, barge_in=True, barge_in_hold_ms=0)
        socket = sockets[0]
        waiting, released = asyncio.Event(), asyncio.Event()
        agent.settings.backend = "gemini_live"
        agent.settings.gemini_api_key = "synthetic-key"
        agent.settings.barge_in = True
        agent.settings.barge_in_hold_ms = 0
        agent.settings.listen_only_uids = []

        async def drain(timeout=30):
            waiting.set()
            await released.wait()
            return {"ok": True}
        playback.wait_tts_complete = drain
        try:
            def message(text):
                return {"serverContent": {"outputTranscription": {"text": text},
                    "inputTranscription": {"text": "question " + text},
                    "modelTurn": {"parts": [{"inlineData": {
                        "data": base64.b64encode(b"\x00\x40" * 480).decode(),
                        "mimeType": "audio/pcm;rate=24000"}}]}, "turnComplete": True}}
            await socket.deliver(message("first"))
            await asyncio.wait_for(waiting.wait(), 1)
            await playback.capture("u", b"\x00\x40" * 320)
            released.set()
            assert await agent.wait_for_reply_end(1)
            await socket.deliver(message("second"))
            assert await agent.wait_for_reply_end(1)
            assert playback.chunks and playback.stops == 1
            assert {"role": "assistant", "content": "second"} in agent.memory.as_messages()
            assert {"role": "assistant", "content": "first"} not in agent.memory.as_messages()
        finally:
            released.set()
            await agent.leave()

    asyncio.run(run())


def test_cascade_drain_interruption_records_input_without_full_assistant_reply(tmp_path):
    async def run():
        agent = make_voice_agent(tmp_path)
        playback = Playback()
        waiting, released = asyncio.Event(), asyncio.Event()

        from dialogue import create_expression_job
        from dialogue.models import Recognition
        class Model:
            name = "synthetic"
            async def recognize(self, audio, permit):
                return Recognition(audio.input_ref, audio.session, permit.attempt_id, audio.user_key,
                                   "recognized", "original question")
            def generate(self, intent, context, **kwargs):
                async def produce(emit):
                    await emit("text_final", text="unplayed tail")
                    await emit("audio", pcm16=b"\x00\x40" * 480)
                return create_expression_job(intent.action_id, intent.session, produce, deadline=intent.deadline)
            async def aclose(self): pass

        async def drain(timeout=30):
            waiting.set()
            await released.wait()
            return {"ok": True}

        agent.backend = Model()
        agent.bind_duplex(playback)
        playback.wait_tts_complete = drain
        await agent.join("area", "room")
        task = asyncio.create_task(agent._handle_utterance("original-member", b"\x01\x00" * 16000, 16000))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            await agent._interrupt_speaking()
            released.set()
            await asyncio.wait_for(task, 1)
            assert agent.memory.as_messages() == [{"role": "user", "content": "original question"}]
            assert agent.memory.recent()[0]["user_key"] == "oopz:original-member"
        finally:
            released.set()
            await agent.leave()
    asyncio.run(run())


def test_tts_failure_preserves_recognized_input_in_real_history(tmp_path, monkeypatch):
    async def run():
        from support.dialogue_fixtures import mimo_backend
        agent = make_voice_agent(tmp_path)
        agent.bind_duplex(Playback())
        backend = mimo_backend(agent.settings)
        agent.backend = backend

        async def asr(*args, **kwargs):
            return "recognized input"
        async def chat(*args, **kwargs):
            return "assistant draft"
        async def fail_tts(*args, **kwargs):
            raise OSError("synthetic provider unavailable")
        monkeypatch.setattr(type(backend), "asr", asr)
        monkeypatch.setattr(type(backend), "chat", chat)
        monkeypatch.setattr(type(backend), "tts", fail_tts)
        await agent.join("area", "room")
        try:
            await agent._handle_utterance("speaker", b"\x00\x40" * 16000, 16000)
            assert agent.memory.as_messages() == [{"role": "user", "content": "recognized input"}]
            assert agent.memory.recent()[0]["user_key"] == "oopz:speaker"
        finally:
            await agent.leave()
            await backend.aclose()
    asyncio.run(run())
