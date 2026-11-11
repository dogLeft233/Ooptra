"""应用把渠道实际播放结果记入真实本地历史；模型完成不代表播放完成。"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json

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
