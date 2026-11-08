"""公开渠道绑定接口：真实音频pump，替代外部bot/浏览器传输。"""
import asyncio
from types import SimpleNamespace

from channels import create_output_controls, create_voice_binding
from channels.models import JoinRoom, RoomTarget


class Transport:
    def __init__(self):
        self.closed = False
        self.callback = None
        self.audio = []

    async def start(self): pass
    async def close(self): self.closed = True
    def set_remote_pcm_callback(self, callback): self.callback = callback
    async def enable_listen(self, enabled=True): return {"ok": True}
    async def push_tts_pcm(self, pcm, rate, **kwargs):
        self.audio.append(pcm)
        return {"ok": True}
    async def stop_tts(self): return {"ok": True}


def bot(transport):
    async def join(**kwargs): return SimpleNamespace(rtc_channel_name="rtc")
    async def leave(): pass
    return SimpleNamespace(voice=SimpleNamespace(backend=transport, join=join, leave=leave))


def binding(platform, received):
    controls = None
    instance = create_voice_binding(platform, on_audio=lambda *frame:received.append(frame), controls=lambda:controls)
    controls = create_output_controls(SimpleNamespace(output_volume=50), lambda:instance.output,
                                      lambda:instance.channel.snapshot().confirmed)
    return instance


def test_rebinding_closes_old_capture_and_rejects_its_late_audio():
    async def scenario():
        old, new = Transport(), Transport()
        received = []
        instance = binding(bot(old), received)
        await instance.prepare_output()
        old_callback = old.callback
        old_channel = instance.channel
        await old_channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))
        instance.bind(bot(new))
        assert instance.output is None
        assert not instance.channel.snapshot().confirmed
        await instance.prepare_output()
        await asyncio.sleep(0)
        assert old.closed and old.callback is None
        old_callback("u", b"\x00\x01", 16000)
        new.callback("v", b"\x00\x02", 16000)
        for _ in range(5):
            await asyncio.sleep(0)
        assert received == [("v", b"\x00\x02", 16000)]
        await instance.close()
    asyncio.run(scenario())


def test_same_binding_is_idempotent_and_preserves_existing_room():
    async def scenario():
        transport = Transport()
        platform = bot(transport)
        instance = binding(platform, [])
        output = await instance.prepare_output()
        channel = instance.channel
        result = await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))
        instance.bind(platform)
        assert instance.channel is channel
        assert instance.output is output
        assert instance.channel.snapshot().session == result.session
        assert not transport.closed
        await instance.close()
    asyncio.run(scenario())
