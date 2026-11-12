"""文字与音频是独立动作；仅替代外部SDK服务，不使用真实账号。"""
import asyncio
from types import SimpleNamespace

import pytest

from channels import create_oopz_channel
from channels.models import JoinRoom, RoomState, RoomTarget, SendText


class Platform:
    def __init__(self):
        self.sent = []
        self.leaves = 0
        self.mode = "ok"
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.bot = SimpleNamespace(voice=SimpleNamespace(join=self.join, leave=self.leave),
            messages=SimpleNamespace(send_message=self.send), areas=SimpleNamespace(get_area_channels=self.channels))
    async def join(self, **kwargs):
        return SimpleNamespace(rtc_channel_name="rtc")
    async def leave(self):
        self.leaves += 1
    async def channels(self, area):
        return [SimpleNamespace(channels=[SimpleNamespace(channel_id="text", channel_type="TEXT"),
                                          SimpleNamespace(channel_id="voice", channel_type="VOICE")])]
    async def send(self, text, *, area, channel):
        self.sent.append((text, area, channel))
        self.entered.set()
        if self.mode == "wait":
            await self.release.wait()
        if self.mode == "fail":
            raise OSError("external SDK payload must not enter result")
        return SimpleNamespace(message_id="message-1")


async def connected(platform):
    channel = create_oopz_channel(platform.bot, lambda: None)
    session = (await channel.execute(JoinRoom("join", RoomTarget("a", "voice"), 0))).session
    return channel, session


def test_text_send_uses_explicit_target_and_same_id_never_repeats_external_send():
    async def run():
        platform = Platform()
        channel, session = await connected(platform)
        command = SendText("subtitle", session, RoomTarget("a", "text"), "字幕", "speech")
        first = await channel.execute(command)
        assert first.status == "completed"
        assert first.media == "text" and first.message_id == "message-1"
        assert first.target == RoomTarget("a", "text") and first.parent_action_id == "speech"
        assert channel.outcome("subtitle") == first
        assert await channel.execute(command) == first
        assert platform.sent == [("字幕", "a", "text")]
    asyncio.run(run())


@pytest.mark.parametrize("target", ["voice", "missing"])
def test_voice_or_unknown_channel_is_rejected_without_guessing_a_destination(target):
    async def run():
        platform = Platform()
        channel, session = await connected(platform)
        result = await channel.execute(SendText("subtitle", session, RoomTarget("a", target), "字幕"))
        assert result.status == "rejected" and result.reason == "target_unavailable"
        assert channel.outcome("subtitle") == result and platform.sent == []
    asyncio.run(run())


def test_uncertain_send_is_retained_and_never_automatically_retried():
    async def run():
        platform = Platform()
        platform.mode = "fail"
        channel, session = await connected(platform)
        command = SendText("subtitle", session, RoomTarget("a", "text"), "字幕")
        result = await channel.execute(command)
        assert result.status == "failed" and result.reason == "effects_unconfirmed"
        platform.mode = "ok"
        assert await channel.execute(command) == result
        assert len(platform.sent) == 1
        assert channel.snapshot().state is RoomState.JOINED
    asyncio.run(run())


def test_cancelling_text_send_retains_unknown_effect_without_leaving_room():
    async def run():
        platform = Platform()
        platform.mode = "wait"
        channel, session = await connected(platform)
        command = SendText("subtitle", session, RoomTarget("a", "text"), "字幕")
        task = asyncio.create_task(channel.execute(command))
        await platform.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        outcome = channel.outcome("subtitle")
        assert outcome.status == "cancelled" and outcome.reason == "effects_unconfirmed"
        assert channel.snapshot().session == session and channel.snapshot().state is RoomState.JOINED
        assert platform.leaves == 0
        assert await channel.execute(command) == outcome
        assert len(platform.sent) == 1
    asyncio.run(run())


def test_old_session_text_cannot_send_after_room_replacement_and_keeps_media_identity():
    async def run():
        platform = Platform()
        channel, old = await connected(platform)
        await channel.execute(JoinRoom("new-room", RoomTarget("a", "voice"), channel.snapshot().revision))
        result = await channel.execute(SendText("stale", old, RoomTarget("a", "text"), "old text", "old-audio"))
        assert result.status == "rejected" and result.reason == "stale_session"
        assert result.media == "text" and result.parent_action_id == "old-audio"
        assert platform.sent == []
    asyncio.run(run())
