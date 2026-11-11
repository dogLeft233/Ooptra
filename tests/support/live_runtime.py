"""Real application and generation flow with only platform/audio/socket boundaries replaced."""
import asyncio
import base64
import json
from types import SimpleNamespace

from app.voice import VoiceCoordinator
from dialogue import create_gemini_backend
from dialogue.models import LiveLimits
from operations.models import VoiceAgentSettings, VoiceApiSettings


class Socket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.previous = None

    async def send(self, text):
        value = json.loads(text)
        self.sent.append(value)
        if "setup" in value:
            await self.queue.put(({"setupComplete": {}}, None))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.previous is not None and not self.previous.done():
            self.previous.set_result(None)
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        value, self.previous = item
        return json.dumps(value)

    async def deliver(self, value):
        completed = asyncio.get_running_loop().create_future()
        await self.queue.put((value, completed))
        await asyncio.wait_for(completed, 1)
        await settle()

    async def close(self):
        self.closed = True
        await self.queue.put(None)


class Playback:
    transport = None
    output_volume = 50
    output_muted = False

    def __init__(self):
        self.chunks = []
        self.stops = 0
        self.handler = None
        self.drain = asyncio.Event()
        self.drain.set()

    def set_remote_pcm_handler(self, handler):
        self.handler = handler

    async def capture(self, uid, pcm, rate=16000):
        assert self.handler is not None
        await self.handler(uid, pcm, rate)
        await settle()

    async def start(self):
        pass

    async def close(self):
        pass

    async def enable_listen(self, enabled=True):
        return {"ok": True}

    async def push_tts_pcm(self, pcm, rate, *, finish=False):
        if pcm:
            self.chunks.append(pcm)
        return {"ok": True}

    async def wait_tts_complete(self, timeout=30):
        await self.drain.wait()
        return {"ok": True}

    async def stop_tts(self):
        self.stops += 1
        self.chunks.clear()
        self.drain.set()
        return {"ok": True}

    async def set_output_volume(self, volume):
        self.output_volume = volume
        return {"ok": True}


async def settle(rounds=40):
    for _ in range(rounds):
        await asyncio.sleep(0)


async def make_live_agent(tmp_path, *, subtitle_target=None, **overrides):
    defaults = dict(enabled=True, backend="gemini_live", gemini_api_key="synthetic", reply_probability_percent=100,
                    voice_leave_enabled=False, voice_volume_enabled=False, listen_only_uids=[],
                    memory_path=str(tmp_path / "memory.jsonl"))
    defaults.update(overrides)
    async def join(**kwargs):
        return SimpleNamespace(rtc_channel_name="rtc")
    async def leave():
        pass
    text_messages = []
    async def channels(area):
        return [SimpleNamespace(channels=[SimpleNamespace(channel_id="text", channel_type="TEXT")])]
    async def send(text, *, area, channel):
        text_messages.append((text, area, channel))
        return SimpleNamespace(message_id="text-message")
    bot = SimpleNamespace(voice=SimpleNamespace(backend=None, join=join, leave=leave),
                          areas=SimpleNamespace(get_area_channels=channels),
                          messages=SimpleNamespace(send_message=send))
    agent = VoiceCoordinator(VoiceAgentSettings(**defaults), VoiceApiSettings(), bot=bot,
                             subtitle_target=lambda:subtitle_target)
    sockets = []
    async def connect(*args, **kwargs):
        socket = Socket()
        sockets.append(socket)
        return socket
    await agent.backend.aclose()
    agent.backend = create_gemini_backend(agent.settings,
                                         connector=connect, limits=LiveLimits(late_transcription_seconds=.1))
    playback = Playback()
    playback.text_messages = text_messages
    agent.bind_duplex(playback)
    await agent.join("area", "room")
    return agent, playback, sockets


def audio_message(pcm=b"\1\0" * 240, *, user_text="", text="", done=False, interrupted=False):
    content = {"modelTurn": {"parts": [{"inlineData": {"data": base64.b64encode(pcm).decode(),
               "mimeType": "audio/pcm;rate=24000"}}]}}
    if user_text:
        content["inputTranscription"] = {"text": user_text}
    if text:
        content["outputTranscription"] = {"text": text}
    if done:
        content["turnComplete"] = True
    if interrupted:
        content["interrupted"] = True
    return {"serverContent": content}
