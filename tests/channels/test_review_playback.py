import asyncio
import time
from types import SimpleNamespace

from channels import create_oopz_channel
from channels.models import JoinRoom, OutputFrame, PlaybackRequest, RoomTarget


def test_muted_output_never_becomes_confirmed_playback():
    async def scenario():
        async def join(**kwargs):
            return SimpleNamespace(rtc_channel_name="rtc")
        class Output:
            async def push_tts_pcm(self, *args, **kwargs): return {"ok": True}
            async def wait_tts_complete(self, timeout): return {"ok": True}
            async def stop_tts(self): return {"ok": True}
        channel = create_oopz_channel(SimpleNamespace(voice=SimpleNamespace(join=join)),
            lambda: Output(), controls=lambda: SimpleNamespace(muted=True))
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        async def frames():
            yield OutputFrame("speech", session, "out", 0, b"\1\0", 24000)
        result = await channel.play(PlaybackRequest("speech", session, "out", time.monotonic()+60), frames())
        assert result.status != "completed"
        assert result.confirmed_samples == 0
        assert result.reason == "output_muted"
    asyncio.run(scenario())


def test_playback_drain_can_use_more_than_ten_seconds():
    from test_playback_contract import Transport

    from channels import create_channel
    from channels.models import AudioPushResult
    async def scenario():
        class SlowDrain(Transport):
            async def wait_playback(self, timeout):
                await asyncio.sleep(10.1)
                return AudioPushResult(True)
        channel = create_channel(SlowDrain())
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        async def frames():
            yield OutputFrame("speech", session, "out", 0, b"\1\0", 24000)
        result = await channel.play(PlaybackRequest("speech", session, "out", time.monotonic()+60), frames())
        assert result.status == "completed"
        assert result.confirmed_samples == 1
    asyncio.run(scenario())
