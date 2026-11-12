"""真实应用音频入口验证抢话门限；仅替换平台、WebSocket 和扬声器。"""
import asyncio

from support.live_runtime import audio_message, make_live_agent, wait_until

LOUD_FRAME = b"\x00\x40" * 320
QUIET_FRAME = b"\x00\x01" * 320
FRAMES_TO_HOLD = 15


async def begin(tmp_path, *, hold_ms=300):
    agent, playback, sockets = await make_live_agent(tmp_path, barge_in=True, barge_in_hold_ms=hold_ms)
    await sockets[0].deliver(audio_message())
    await wait_until(lambda: bool(playback.chunks))
    assert playback.chunks, "必须先产生真实播放，零次打断的断言才有意义"
    return agent, playback, sockets[0]


async def capture(playback, count, *, uid="u1", frame=LOUD_FRAME):
    for _ in range(count):
        await playback.capture(uid, frame)


def test_short_blip_does_not_barge_in(tmp_path):
    async def run():
        agent, playback, socket = await begin(tmp_path)
        try:
            await capture(playback, FRAMES_TO_HOLD - 5)
            assert playback.stops == 0
            audio_inputs = [event for event in socket.sent if "audio" in event.get("realtimeInput", {})]
            assert len(audio_inputs) == FRAMES_TO_HOLD - 5
        finally:
            await agent.leave()
    asyncio.run(run())


def test_sustained_speech_barges_in_once(tmp_path):
    async def run():
        agent, playback, _ = await begin(tmp_path)
        try:
            await capture(playback, FRAMES_TO_HOLD * 3)
            assert playback.stops == 1
        finally:
            await agent.leave()
    asyncio.run(run())


def test_a_gap_resets_the_counter(tmp_path):
    async def run():
        agent, playback, _ = await begin(tmp_path)
        try:
            await capture(playback, 10)
            await capture(playback, 1, frame=QUIET_FRAME)
            await capture(playback, 10)
            assert playback.stops == 0
        finally:
            await agent.leave()
    asyncio.run(run())


def test_quiet_frames_never_accumulate(tmp_path):
    async def run():
        agent, playback, socket = await begin(tmp_path)
        try:
            await capture(playback, FRAMES_TO_HOLD * 5, frame=QUIET_FRAME)
            assert playback.stops == 0
            assert len([event for event in socket.sent if "audio" in event.get("realtimeInput", {})]) == FRAMES_TO_HOLD * 5
        finally:
            await agent.leave()
    asyncio.run(run())


def test_hold_ms_zero_restores_immediate_barge_in(tmp_path):
    async def run():
        agent, playback, _ = await begin(tmp_path, hold_ms=0)
        try:
            await capture(playback, 1)
            assert playback.stops == 1
        finally:
            await agent.leave()
    asyncio.run(run())


def test_rearm_clears_accumulated_speech(tmp_path):
    async def run():
        agent, playback, socket = await begin(tmp_path)
        try:
            await capture(playback, FRAMES_TO_HOLD - 1)
            await socket.deliver({"serverContent": {"turnComplete": True}})
            await agent.wait_for_reply_end(timeout=1)
            await socket.deliver(audio_message())
            await capture(playback, 5)
            assert playback.stops == 0
        finally:
            await agent.leave()
    asyncio.run(run())


def test_gate_is_per_user(tmp_path):
    async def run():
        agent, playback, _ = await begin(tmp_path)
        try:
            for _ in range(10):
                await capture(playback, 1, uid="u1")
                await capture(playback, 1, uid="u2")
            assert playback.stops == 0
        finally:
            await agent.leave()
    asyncio.run(run())
