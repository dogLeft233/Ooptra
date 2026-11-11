"""应用把渠道实际播放结果记入真实本地历史；模型完成不代表播放完成。"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import threading

import pytest
from support.channel_fixtures import Duplex, make_voice_agent

from dialogue import create_gemini_backend
from dialogue.models import VoiceReply


class Playback(Duplex):
    def __init__(self):
        self.output_volume = 50
        self.output_muted = False
        self.pushed = 0
        self.stopped = 0
        self.handler = None

    def set_remote_pcm_handler(self, handler):
        self.handler = handler

    def enqueue_remote_pcm(self, uid, pcm, sample_rate):
        raise NotImplementedError("该适配通过 capture 显式交付音频")

    async def start(self):
        pass

    async def set_output_volume(self, volume):
        self.output_volume = volume
        return {"ok": True}

    async def push_tts_pcm(self, pcm16, sample_rate, *, finish=False):
        self.pushed += len(pcm16)
        return {"ok": True}

    async def wait_tts_complete(self, timeout: float = 30):
        return {"ok": True}

    async def stop_tts(self):
        self.stopped += 1
        return {"ok": True}

    async def capture(self, uid, pcm):
        assert self.handler is not None
        await self.handler(uid, pcm, 16000)


class LiveSocket:
    """仅替代外部WebSocket；deliver等待真实生成reader处理上一条消息。"""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.previous = None
        self.closed = False

    async def send(self, text):
        if "setup" in json.loads(text):
            await self.queue.put(({"setupComplete": {}}, None))

    async def deliver(self, message):
        acknowledged = asyncio.get_running_loop().create_future()
        await self.queue.put((message, acknowledged))
        await asyncio.wait_for(acknowledged, 1)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.previous is not None and not self.previous.done():
            self.previous.set_result(None)
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        message, self.previous = item
        return json.dumps(message)

    async def close(self):
        self.closed = True
        await self.queue.put(None)


def test_client_interruption_never_records_the_full_generated_reply(tmp_path):
    async def run():
        agent = make_voice_agent(tmp_path)
        playback, socket = Playback(), LiveSocket()
        agent.settings.backend = "gemini_live"
        agent.settings.gemini_api_key = "synthetic-key"
        agent.settings.barge_in = True
        agent.settings.barge_in_hold_ms = 0
        agent.settings.listen_only_uids = []

        async def connect(*args, **kwargs):
            return socket

        from behavior import create_reply_policy

        agent.backend = create_gemini_backend(agent.settings, agent.memory,
                                              reply_policy=create_reply_policy(), connector=connect)
        agent.live_mode = True
        agent.bind_duplex(playback)
        try:
            await agent.join("area", "room")
            await socket.deliver({"serverContent": {
                "inputTranscription": {"text": "question"},
                "outputTranscription": {"text": "complete generated answer"},
                "modelTurn": {"parts": [{"inlineData": {
                    "data": base64.b64encode(b"\x00\x40" * 480).decode(),
                    "mimeType": "audio/pcm;rate=24000",
                }}]},
            }})
            assert playback.pushed > 0
            await playback.capture("u", b"\x00\x40" * 320)
            assert playback.stopped == 1
            # NO_INTERRUPTION means the server does not mark this local interruption.
            await socket.deliver({"serverContent": {"turnComplete": True}})
            assert agent.memory.as_messages() == [{"role": "user", "content": "question"}]
        finally:
            with contextlib.suppress(Exception):
                await agent.leave()
            await agent.backend.aclose()
    asyncio.run(run())


def test_same_room_context_refresh_rejects_late_cascade_result(tmp_path, monkeypatch):
    async def run():
        agent = make_voice_agent(tmp_path)
        playback = Playback()
        started = asyncio.Event()

        class SlowModel:
            async def handle_utterance(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # A provider may complete while local cancellation arrives.
                    return VoiceReply(user_text="old source question", text="stale reply",
                                      pcm16=b"\x00\x40" * 480, sample_rate=24000)

            async def aclose(self):
                pass

        monkeypatch.setattr(agent, "backend", SlowModel())
        agent.bind_duplex(playback)
        try:
            await agent.join("area", "room")
            original_session = agent.channels.snapshot().session
            pending = asyncio.create_task(agent._handle_utterance("u", b"\x00\x40" * 320, 16000))
            await started.wait()
            await agent.refresh_astrbot_context()
            await pending
            assert agent.channels.snapshot().session == original_session, "this case refreshes a source in the same room"
            assert playback.pushed == 0, "old source output must not play after context refresh"
            assert agent.memory.as_messages() == [], "old source output must not enter the refreshed history"
        finally:
            with contextlib.suppress(Exception):
                await agent.leave()
    asyncio.run(run())


def test_tail_playback_interruption_does_not_suppress_the_next_live_reply(tmp_path):
    async def run():
        agent = make_voice_agent(tmp_path)
        playback, socket = Playback(), LiveSocket()
        waiting, released = asyncio.Event(), asyncio.Event()
        agent.settings.backend = "gemini_live"
        agent.settings.gemini_api_key = "synthetic-key"
        agent.settings.barge_in = True
        agent.settings.barge_in_hold_ms = 0
        agent.settings.listen_only_uids = []

        async def connect(*args, **kwargs):
            return socket

        async def drain(timeout=30):
            waiting.set()
            await released.wait()
            return {"ok": True}

        from behavior import create_reply_policy
        agent.backend = create_gemini_backend(agent.settings, agent.memory,
                                              reply_policy=create_reply_policy(), connector=connect)
        agent.live_mode = True
        agent.bind_duplex(playback)
        playback.wait_tts_complete = drain
        try:
            await agent.join("area", "room")
            def message(text):
                return {"serverContent": {"outputTranscription": {"text": text},
                    "inputTranscription": {"text": "question " + text},
                    "modelTurn": {"parts": [{"inlineData": {
                        "data": base64.b64encode(b"\x00\x40" * 480).decode(),
                        "mimeType": "audio/pcm;rate=24000"}}]}, "turnComplete": True}}
            await socket.deliver(message("first"))
            await asyncio.wait_for(waiting.wait(), 1)
            first_bytes = playback.pushed
            await playback.capture("u", b"\x00\x40" * 320)
            released.set()
            assert await agent.wait_for_reply_end(1)
            await socket.deliver(message("second"))
            assert await agent.wait_for_reply_end(1)
            assert playback.pushed > first_bytes
            assert {"role": "assistant", "content": "second"} in agent.memory.as_messages()
            assert {"role": "assistant", "content": "first"} not in agent.memory.as_messages()
        finally:
            released.set()
            await agent.leave()
            await agent.backend.aclose()
    asyncio.run(run())


def test_cascade_drain_interruption_records_input_without_full_assistant_reply(tmp_path):
    async def run():
        agent = make_voice_agent(tmp_path)
        playback = Playback()
        waiting, released = asyncio.Event(), asyncio.Event()

        class Model:
            async def handle_utterance(self, *args, **kwargs):
                return VoiceReply(user_text="original question", text="unplayed tail", pcm16=b"\x00\x40" * 480)
            async def aclose(self):
                pass

        async def drain(timeout=30):
            waiting.set()
            await released.wait()
            return {"ok": True}

        agent.backend = Model()
        agent.bind_duplex(playback)
        playback.wait_tts_complete = drain
        await agent.join("area", "room")
        task = asyncio.create_task(agent._handle_utterance("original-member", b"\x01\x00", 16000))
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


def test_tts_failure_preserves_recognized_input_in_real_history(tmp_path):
    async def run():
        from support.dialogue_fixtures import mimo_backend
        agent = make_voice_agent(tmp_path)
        agent.bind_duplex(Playback())
        backend = mimo_backend(agent.settings, agent.memory)
        agent.backend = backend

        async def asr(*args):
            return "recognized input"
        async def chat(*args, **kwargs):
            return "assistant draft"
        async def fail_tts(*args):
            raise OSError("synthetic provider unavailable")
        backend.asr, backend.chat, backend.tts = asr, chat, fail_tts
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
def test_stop_after_nonstream_recognition_preserves_only_original_input(tmp_path, stage):
    async def run():
        from support.dialogue_fixtures import mimo_backend
        agent = make_voice_agent(tmp_path)
        agent.bind_duplex(Playback())
        backend = mimo_backend(agent.settings, agent.memory)
        agent.backend = backend
        waiting = asyncio.Event()

        async def asr(*args):
            return "recognized before stop"
        async def chat(*args, **kwargs):
            if stage == "chat":
                waiting.set()
                await asyncio.Event().wait()
            return "unplayed assistant draft"
        async def tts(*args):
            waiting.set()
            await asyncio.Event().wait()
        backend.asr, backend.chat, backend.tts = asr, chat, tts
        await agent.join("area", "room")
        try:
            pending = asyncio.create_task(agent._handle_utterance("speaker", b"\x00\x40" * 16000, 16000))
            await asyncio.wait_for(waiting.wait(), 1)
            await agent.stop_current_reply()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
            assert agent.memory.as_messages() == [{"role": "user", "content": "recognized before stop"}]
            rows = [json.loads(line) for line in (tmp_path / "memory.jsonl").read_text(encoding="utf-8").splitlines()]
            assert len(rows) == 1
            assert rows[0]["user_key"] == "oopz:speaker"
            assert agent.duplex.pushed == 0
        finally:
            await agent.leave()
            await backend.aclose()
    asyncio.run(run())


def test_live_announcement_records_confirmed_assistant_without_human_input(tmp_path):
    async def run():
        agent = make_voice_agent(tmp_path)
        playback, socket = Playback(), LiveSocket()
        sent = asyncio.Event()
        original_send = socket.send
        async def send(text):
            await original_send(text)
            if "realtimeInput" in json.loads(text):
                sent.set()
        socket.send = send
        agent.settings.backend = "gemini_live"
        agent.settings.gemini_api_key = "synthetic-key"
        async def connect(*args, **kwargs):
            return socket
        from behavior import create_reply_policy
        agent.backend = create_gemini_backend(agent.settings, agent.memory,
            reply_policy=create_reply_policy(), connector=connect)
        agent.live_mode = True
        agent.bind_duplex(playback)
        await agent.join("area", "room")
        try:
            pending = asyncio.create_task(agent.speak_announcement("enter", "大家好"))
            await asyncio.wait_for(sent.wait(), 1)
            await socket.deliver({"serverContent": {
                "outputTranscription": {"text": "大家好"},
                "modelTurn": {"parts": [{"inlineData": {
                    "data": base64.b64encode(b"\x00\x40" * 480).decode(),
                    "mimeType": "audio/pcm;rate=24000"}}]}, "turnComplete": True}})
            result = await asyncio.wait_for(pending, 1)
            assert result["ok"], result
            assert playback.pushed > 0
            assert agent.memory.as_messages() == [{"role": "assistant", "content": "大家好"}]
            rows = [json.loads(line) for line in (tmp_path / "memory.jsonl").read_text(encoding="utf-8").splitlines()]
            assert len(rows) == 1 and rows[0]["role"] == "assistant"
        finally:
            await agent.leave()
            await agent.backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("streaming", [False, True])
def test_stop_during_accepted_history_write_commits_once(tmp_path, monkeypatch, streaming):
    async def run():
        from support.context_fixtures import prepare_source
        agent = make_voice_agent(tmp_path)
        agent.bind_duplex(Playback())
        entered, released = threading.Event(), threading.Event()
        original = agent.memory.record
        def blocking_record(turn):
            entered.set()
            assert released.wait(3), "test must release the real disk write"
            return original(turn)
        monkeypatch.setattr(agent.memory, "record", blocking_record)
        class Model:
            async def handle_utterance(self, *args, on_recognition=None, **kwargs):
                on_recognition("question")
                return VoiceReply(user_text="question", text="answer", pcm16=b"\x00\x40" * 480)
            async def aclose(self):
                pass
        class StreamingModel(Model):
            async def handle_utterance_stream(self, *args, on_audio, on_recognition, **kwargs):
                on_recognition("question")
                await on_audio(b"\x00\x40" * 480, 24000)
                return VoiceReply(user_text="question", text="answer", sample_rate=24000)
        agent.backend = StreamingModel() if streaming else Model()
        await agent.join("area", "room")
        await prepare_source(agent.context_bridge, {"ok": True, "provider": "memory_companion",
            "memory_scopes": [{"umo": "original"}]}, "area", "room")
        try:
            pending = asyncio.create_task(agent._handle_utterance("speaker", b"\x01\x00", 16000))
            assert await asyncio.to_thread(entered.wait, 1)
            cancelled = asyncio.Event()
            original_cancel = pending.cancel
            def cancel(*args, **kwargs):
                accepted = original_cancel(*args, **kwargs)
                cancelled.set()
                return accepted
            monkeypatch.setattr(pending, "cancel", cancel)
            stopping = asyncio.create_task(agent.stop_current_reply())
            await asyncio.wait_for(cancelled.wait(), 1)
            released.set()
            await asyncio.wait_for(stopping, 1)
            with contextlib.suppress(asyncio.CancelledError):
                await pending
            assert agent.memory.as_messages() == [
                {"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}]
            assert agent.context_bridge.status()["pending_writes"] == 1
            events = await agent.context_bridge.poll(wait=0)
            commits = [event for event in events if event["operation"] == "commit"]
            assert len(commits) == 1
            assert commits[0]["payload"]["reply_text"] == "answer"
        finally:
            released.set()
            await agent.leave()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["stop", "leave", "refresh"])
def test_live_drain_cancel_preserves_only_input_in_original_scope(tmp_path, operation):
    async def run():
        from support.context_fixtures import prepare_source
        agent = make_voice_agent(tmp_path)
        playback, socket = Playback(), LiveSocket()
        waiting, released = asyncio.Event(), asyncio.Event()
        first_connection = True
        async def connect(*args, **kwargs):
            nonlocal first_connection
            if first_connection:
                first_connection = False
                return socket
            return LiveSocket()
        async def drain(timeout=30):
            waiting.set()
            await released.wait()
            return {"ok": True}
        from behavior import create_reply_policy
        agent.settings.backend = "gemini_live"
        agent.settings.gemini_api_key = "synthetic"
        agent.backend = create_gemini_backend(agent.settings, agent.memory,
            reply_policy=create_reply_policy(), connector=connect)
        agent.live_mode = True
        agent.bind_duplex(playback)
        playback.wait_tts_complete = drain
        await agent.join("area", "room")
        await prepare_source(agent.context_bridge, {"ok": True, "provider": "memory_companion",
            "memory_scopes": [{"umo": "original"}]}, "area", "room")
        await agent._wire_live()
        async def worker():
            while True:
                for event in await agent.context_bridge.poll(wait=0):
                    if event["operation"] == "prepare":
                        agent.context_bridge.resolve(event["request_id"], {"ok": True,
                            "provider": "memory_companion", "memory_scopes": [{"umo": "new"}]})
                await asyncio.sleep(0)
        worker_task = asyncio.create_task(worker())
        try:
            await socket.deliver({"serverContent": {
                "inputTranscription": {"text": "recognized original input"},
                "outputTranscription": {"text": "unconfirmed assistant tail"},
                "modelTurn": {"parts": [{"inlineData": {
                    "data": base64.b64encode(b"\x00\x40" * 480).decode(),
                    "mimeType": "audio/pcm;rate=24000"}}]}, "turnComplete": True}})
            await asyncio.wait_for(waiting.wait(), 1)
            if operation == "stop":
                await agent.stop_current_reply()
            elif operation == "leave":
                await agent.leave()
            else:
                await agent.refresh_astrbot_context()
            released.set()
            assert agent.memory.as_messages() == [
                {"role": "user", "content": "recognized original input"}]
            commits = [event for event in json.loads(
                (tmp_path / "astrbot_voice_outbox.json").read_text(encoding="utf-8"))
                       if event["operation"] == "commit"]
            assert len(commits) == 1
            assert commits[0]["scope"]["memory_scopes"] == [{"umo": "original"}]
            assert commits[0]["payload"]["user_text"] == "recognized original input"
            assert commits[0]["payload"]["reply_text"] == ""
        finally:
            released.set()
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
            await agent.leave()
            await agent.backend.aclose()
    asyncio.run(run())
