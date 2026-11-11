"""公开播放能力以真实渠道会话及外部传输替身验证实际完成。"""
import asyncio

import pytest
from support.channel_fixtures import Duplex

from channels import create_channel, create_playback
from channels.models import AudioPushResult, JoinRoom, RoomTarget


class Transport:
    async def join(self, target):
        return "rtc"
    async def leave(self):
        pass
    async def retire_output(self):
        pass
    async def push_audio(self, pcm, rate, *, finish=False):
        return AudioPushResult(True)


@pytest.mark.parametrize("event", ["completed", "interrupted", "muted", "changed_room", "failed"])
def test_playback_confirmation_rechecks_outcome_after_drain(event):
    async def run():
        channel = create_channel(Transport())
        joined = await channel.execute(JoinRoom("join", RoomTarget("a", "c"), 0))
        output = Duplex()
        waiting, release = asyncio.Event(), asyncio.Event()
        stopped = []

        async def drain(timeout):
            waiting.set()
            await release.wait()
            return {"ok": event != "failed"}
        async def stop():
            stopped.append(True)
            release.set()
            return {"ok": True}
        output.wait_tts_complete, output.stop_tts = drain, stop
        playback = create_playback(channel, joined.session, output)
        assert (await playback.push(b"\x01\x00", 24000)).ok
        finished = asyncio.create_task(playback.finish(24000))
        await asyncio.wait_for(waiting.wait(), 1)
        if event == "interrupted":
            await asyncio.wait_for(playback.stop(), 1)
        elif event == "muted":
            output.output_muted = True
        elif event == "changed_room":
            await channel.execute(JoinRoom("next", RoomTarget("a", "new"), channel.snapshot().revision))
            await playback.stop()
            assert not stopped
        release.set()
        result = await asyncio.wait_for(finished, 1)
        assert result.submitted_bytes == 2
        assert result.status == {"muted": "silent", "changed_room": "interrupted"}.get(event, event)
    asyncio.run(run())
