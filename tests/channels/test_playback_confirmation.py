"""真实渠道公开播放能力重查排空后的终态，不让生成完成冒充播放完成。"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from channels import create_oopz_channel
from channels.models import JoinRoom, OutputFrame, PlaybackRequest, RoomTarget


@pytest.mark.parametrize("event", ["completed", "interrupted", "muted", "changed_room", "failed"])
def test_playback_confirmation_rechecks_outcome_after_drain(event):
    async def run():
        waiting, release = asyncio.Event(), asyncio.Event()
        controls = SimpleNamespace(muted=False)
        async def join(**kwargs): return SimpleNamespace(rtc_channel_name="rtc")
        async def leave(): pass
        class Output:
            async def push_tts_pcm(self, *args, **kwargs): return {"ok": True}
            async def wait_tts_complete(self, timeout):
                waiting.set()
                await release.wait()
                return {"ok": event != "failed"}
            async def stop_tts(self):
                release.set()
                return {"ok": True}
        channel = create_oopz_channel(SimpleNamespace(voice=SimpleNamespace(join=join, leave=leave)),
                                     lambda: Output(), controls=lambda: controls)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "c"), 0))).session
        async def frames():
            yield OutputFrame("speech", session, "out", 0, b"\x01\x00", 24000)
        finished = asyncio.create_task(channel.play(
            PlaybackRequest("speech", session, "out", time.monotonic()+5), frames()))
        await asyncio.wait_for(waiting.wait(), 1)
        if event == "interrupted":
            await channel.cancel_current_playback(session)
        elif event == "muted":
            controls.muted = True
        elif event == "changed_room":
            await channel.execute(JoinRoom("next", RoomTarget("a", "new"), channel.snapshot().revision))
            assert await channel.cancel_current_playback(session) is None
        release.set()
        result = await asyncio.wait_for(finished, 1)
        assert result.submitted_samples == 1
        assert (result.status == "completed") == (event == "completed")
        assert result.confirmed_samples == (1 if event == "completed" else 0)
    asyncio.run(run())
