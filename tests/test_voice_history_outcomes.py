"""真实应用按渠道播放事实提交事务历史；生成草稿不能冒充已输出。"""
import asyncio
import base64

import pytest
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


@pytest.mark.parametrize("stage", ["chat", "tts"])
def test_stop_after_nonstream_recognition_preserves_only_original_input(tmp_path, monkeypatch, stage):
    async def run():
        from support.dialogue_fixtures import mimo_backend
        agent = make_voice_agent(tmp_path)
        playback = Playback()
        agent.bind_duplex(playback)
        backend = mimo_backend(agent.settings)
        agent.backend = backend
        waiting = asyncio.Event()
        async def asr(*args, **kwargs): return "recognized before stop"
        async def chat(*args, **kwargs):
            if stage == "chat":
                waiting.set()
                await asyncio.Event().wait()
            return "unplayed assistant draft"
        async def tts(*args, **kwargs):
            waiting.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(type(backend), "asr", asr)
        monkeypatch.setattr(type(backend), "chat", chat)
        monkeypatch.setattr(type(backend), "tts", tts)
        await agent.join("area", "room")
        try:
            pending = asyncio.create_task(agent._handle_utterance("speaker", b"\x00\x40" * 16000, 16000))
            await asyncio.wait_for(waiting.wait(), 1)
            await agent.stop_current_reply()
            await asyncio.gather(pending, return_exceptions=True)
            assert agent.memory.as_messages() == [{"role": "user", "content": "recognized before stop"}]
            assert agent.memory.recent()[0]["user_key"] == "oopz:speaker"
            assert not playback.chunks
        finally:
            await agent.leave()
            await backend.aclose()
    asyncio.run(run())


def test_live_announcement_records_confirmed_assistant_without_human_input(tmp_path):
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path)
        socket = sockets[0]
        sent = asyncio.Event()
        original_send = socket.send
        async def send(text):
            import json
            await original_send(text)
            if "realtimeInput" in json.loads(text):
                sent.set()
        socket.send = send
        try:
            pending = asyncio.create_task(agent.speak_announcement("enter", "大家好"))
            await asyncio.wait_for(sent.wait(), 1)
            await socket.deliver(audio_message(user_text="", text="大家好", done=True))
            result = await asyncio.wait_for(pending, 1)
            assert result["ok"], result
            assert playback.chunks
            assert agent.memory.as_messages() == [{"role": "assistant", "content": "大家好"}]
        finally:
            await agent.leave()
    asyncio.run(run())


@pytest.mark.parametrize("probability", [0, 100])
def test_stop_during_accepted_context_write_collects_original_input_once(tmp_path, monkeypatch, probability):
    import threading

    from support.context_fixtures import prepare_source

    from dialogue import create_expression_job
    from dialogue.models import Recognition
    async def run():
        agent = make_voice_agent(tmp_path)
        playback = Playback()
        agent.bind_duplex(playback)
        agent.settings.reply_probability_percent = probability
        agent.settings.force_reply_keywords = []
        async def recognize(audio, permit):
            return Recognition(audio.input_ref, audio.session, permit.attempt_id, audio.user_key,
                               "recognized", "recognized before commit")
        def generate(intent, prepared, **kwargs):
            async def produce(emit):
                await emit("text_final", text="confirmed reply")
                await emit("audio", pcm16=b"\0\x40" * 320)
            return create_expression_job(intent.action_id, intent.session, produce, deadline=intent.deadline)
        monkeypatch.setattr(agent.backend, "recognize", recognize)
        monkeypatch.setattr(agent.backend, "generate", generate)
        await agent.join("area", "room")
        await prepare_source(agent.context_bridge, {"ok": True, "provider": "memory_companion",
            "memory_scopes": [{"umo": "original"}]}, "area", "room")
        entered, release = threading.Event(), threading.Event()
        method = "record_output" if probability else "finish_input"
        original = getattr(agent.context_service, method)
        def write(value):
            entered.set()
            assert release.wait(3)
            return original(value)
        monkeypatch.setattr(agent.context_service, method, write)
        worker = asyncio.create_task(agent._handle_utterance("speaker", b"\0\x40" * 16000, 16000))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            stopping = asyncio.create_task(agent.stop_current_reply())
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(stopping, 1)
            await asyncio.gather(worker, return_exceptions=True)
            rows = agent.memory.recent()
            assert [row["content"] for row in rows if row["role"] == "user"] == ["recognized before commit"]
            events = await agent.context_bridge.poll(wait=0)
            assert len(events) == 1
            assert events[0]["scope"]["memory_scopes"] == [{"umo": "original"}]
            assert events[0]["payload"]["user_text"] == "recognized before commit"
        finally:
            release.set()
            await agent.stop()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["stop", "leave", "refresh"])
def test_live_drain_cancel_preserves_only_input_in_original_scope(tmp_path, operation):
    from support.context_fixtures import prepare_source
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path)
        await agent._close_live_flow()
        await prepare_source(agent.context_bridge, {"ok": True, "provider": "memory_companion",
            "memory_scopes": [{"umo": "original"}]}, "area", "room")
        await agent._wire_live()
        socket = sockets[-1]
        waiting, release = asyncio.Event(), asyncio.Event()
        async def drain(timeout=30):
            waiting.set()
            await release.wait()
            return {"ok": True}
        playback.wait_tts_complete = drain
        try:
            await socket.deliver(audio_message(user_text="recognized original input",
                text="unconfirmed assistant tail", done=True))
            await asyncio.wait_for(waiting.wait(), 1)
            if operation == "stop":
                await agent.stop_current_reply()
            elif operation == "leave":
                await agent.leave()
            else:
                await agent.refresh_astrbot_context()
            release.set()
            assert agent.memory.as_messages() == [{"role": "user", "content": "recognized original input"}]
            events = await agent.context_bridge.poll(wait=0)
            assert len(events) == 1
            assert events[0]["scope"]["memory_scopes"] == [{"umo": "original"}]
            assert events[0]["payload"]["user_text"] == "recognized original input"
            assert events[0]["payload"]["reply_text"] == ""
        finally:
            release.set()
            await agent.stop()
    asyncio.run(run())
