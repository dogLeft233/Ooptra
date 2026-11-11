"""公开播放口：首段思考预算独立于已播出后的断流预算。"""
import asyncio
import time

import pytest

from channels import create_channel
from channels.models import AudioPushResult, JoinRoom, OutputFrame, PlaybackRequest, RoomTarget


class Transport:
    def __init__(self):
        self.frames = []
    async def join(self, target): return "rtc"
    async def leave(self): pass
    async def retire_output(self): pass
    async def push_audio(self, pcm, rate, *, finish=False):
        self.frames.append(pcm)
        return AudioPushResult(True)
    async def wait_playback(self, timeout): return AudioPushResult(True)


def test_thinking_longer_than_old_ten_second_limit_can_still_play(caplog):
    async def run():
        transport = Transport()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        async def frames():
            # Reproduces the real incident: no audio for over ten seconds.
            await asyncio.sleep(10.2)
            yield OutputFrame("speech", session, "output", 0, b"\0\1", 16000)
        result = await channel.play(PlaybackRequest("speech", session, "output", time.monotonic()+30,
            first_audio_timeout_seconds=20, audio_gap_timeout_seconds=.1), frames())
        assert result.status == "completed" and result.confirmed_samples == 1
        assert transport.frames == [b"\0\1", b""]
    with caplog.at_level("INFO"):
        asyncio.run(run())
    assert "event=voice.playback.first_audio" in caplog.text
    assert "status=completed" in caplog.text


@pytest.mark.parametrize("phase", ["first_audio", "audio_gap", "deadline"])
def test_expiration_identifies_phase_preserves_evidence_and_closes_source(phase, caplog):
    async def run():
        transport = Transport()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        closed = asyncio.Event()
        async def frames():
            try:
                if phase == "audio_gap":
                    yield OutputFrame("speech", session, "output", 0, b"\0\1", 16000)
                await asyncio.Event().wait()
            finally:
                closed.set()
        request = PlaybackRequest("speech", session, "output", time.monotonic()+(.05 if phase == "deadline" else 5),
            first_audio_timeout_seconds=.05 if phase == "first_audio" else 2,
            audio_gap_timeout_seconds=.05)
        result = await channel.play(request, frames())
        assert result.reason == ("deadline" if phase == "deadline" else phase+"_timeout")
        assert result.status == ("partial" if phase == "audio_gap" else "expired")
        assert result.submitted_samples == result.unknown_samples == (1 if phase == "audio_gap" else 0)
        assert result.confirmed_samples == 0 and closed.is_set()
        assert channel.playback("speech") == result
    with caplog.at_level("INFO"):
        asyncio.run(run())
    assert "event=voice.playback.finished" in caplog.text


@pytest.mark.parametrize("field,value", [("first_audio_timeout_seconds", True),
    ("first_audio_timeout_seconds", 0), ("first_audio_timeout_seconds", float('nan')),
    ("first_audio_timeout_seconds", 121), ("audio_gap_timeout_seconds", 61)])
def test_invalid_wait_budgets_cannot_create_playback_request(field, value):
    from channels.models import SessionRef
    session = SessionRef("run", "bind", "session", RoomTarget("a", "b"))
    with pytest.raises(ValueError):
        PlaybackRequest("speech", session, "output", time.monotonic()+5, **{field: value})
