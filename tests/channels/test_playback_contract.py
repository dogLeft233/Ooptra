import asyncio
import time

from channels import create_channel
from channels.models import AudioPushResult, JoinRoom, RoomTarget


class Transport:
    def __init__(self):
        self.frames = []
        self.drain = asyncio.Event()
        self.stopped = 0

    async def join(self, target):
        return "rtc"

    async def leave(self):
        pass

    async def retire_output(self):
        self.stopped += 1

    async def push_audio(self, pcm, rate, *, finish=False):
        self.frames.append((pcm, finish))
        return AudioPushResult(True)

    async def wait_playback(self, timeout):
        await self.drain.wait()
        return AudioPushResult(True)


def test_playback_does_not_complete_until_transport_drains():
    from channels.models import OutputFrame, PlaybackRequest

    async def scenario():
        transport = Transport()
        channel = create_channel(transport)
        joined = await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))
        request = PlaybackRequest("speech", joined.session, "out", time.monotonic() + 5)

        async def frames():
            yield OutputFrame("speech", joined.session, "out", 0, b"\x00\x01", 16000)

        task = asyncio.create_task(channel.play(request, frames()))
        while len(transport.frames) < 2:
            await asyncio.sleep(0)
        assert not task.done()
        assert channel.playback("speech").status == "running"
        transport.drain.set()
        result = await task
        assert result.status == "completed"
        assert result.submitted_samples == result.confirmed_samples == 1
        assert channel.playback("speech") == result
        assert transport.frames == [(b"\x00\x01", False), (b"", True)]
    asyncio.run(scenario())


def test_source_failure_preserves_partial_result_and_does_not_finish():
    from channels.api import OutputSourceError
    from channels.models import OutputFrame, PlaybackRequest

    async def scenario():
        transport = Transport()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        async def frames():
            yield OutputFrame("speech", session, "out", 0, b"\x00\x01", 16000)
            raise OutputSourceError("secret-model-body")
        result = await channel.play(PlaybackRequest("speech", session, "out", time.monotonic()+5), frames())
        assert result.status == "partial"
        assert result.confirmed_samples == 0 and result.unknown_samples == 1
        assert transport.frames == [(b"\x00\x01", False)]
        assert transport.stopped == 1
        assert "secret" not in result.reason
    asyncio.run(scenario())


def test_leave_cancels_waiting_source_and_rejects_old_session_output():
    from channels.models import LeaveRoom, OutputFrame, PlaybackRequest

    async def scenario():
        transport = Transport()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        started, closed = asyncio.Event(), asyncio.Event()
        async def frames():
            try:
                started.set()
                await asyncio.Event().wait()
                yield OutputFrame("speech", session, "out", 0, b"\x00\x01", 16000)
            finally:
                closed.set()
        task = asyncio.create_task(channel.play(PlaybackRequest("speech", session, "out", time.monotonic()+5), frames()))
        await started.wait()
        left = await asyncio.wait_for(channel.execute(LeaveRoom("leave", session)), 1)
        assert left.status == "completed"
        assert (await task).status == "cancelled"
        assert closed.is_set()
        assert transport.frames == []
    asyncio.run(scenario())


class Source:
    def __init__(self):
        self.closed = False
        self.started = False
    def __aiter__(self):
        return self
    async def __anext__(self):
        self.started = True
        await asyncio.Event().wait()
    async def aclose(self):
        self.closed = True


def test_cancel_before_worker_starts_closes_source_and_retains_terminal():
    from channels.models import PlaybackRequest
    async def scenario():
        transport = Transport()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        source = Source()
        play = asyncio.create_task(channel.play(PlaybackRequest("speech", session, "out", time.monotonic()+5), source))
        cancel = asyncio.create_task(channel.cancel_playback("speech"))
        results = await asyncio.gather(play, cancel, return_exceptions=True)
        assert source.closed
        assert channel.playback("speech").status == "cancelled"
        assert all(not isinstance(result, BaseException) for result in results)
    asyncio.run(scenario())


def test_rejected_request_is_queryable_and_cannot_be_reused_as_a_new_play():
    from channels.models import PlaybackRequest
    async def scenario():
        transport = Transport()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        request = PlaybackRequest("expired", session, "out", time.monotonic()-1)
        source = Source()
        result = await channel.play(request, source)
        assert source.closed
        assert channel.playback("expired") == result
        changed = PlaybackRequest("expired", session, "out", time.monotonic()+5)
        assert (await channel.play(changed, Source())).reason == "action_conflict"
        assert channel.playback("expired") == result
    asyncio.run(scenario())


def test_duplicate_wait_with_same_source_never_closes_the_active_stream():
    from channels.models import PlaybackRequest
    async def scenario():
        transport = Transport()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        source = Source()
        request = PlaybackRequest("speech", session, "out", time.monotonic()+5)
        first = asyncio.create_task(channel.play(request, source))
        while not source.started:
            await asyncio.sleep(0)
        duplicate = asyncio.create_task(channel.play(request, source))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not source.closed
        await channel.cancel_playback("speech")
        assert (await first) == (await duplicate)
        assert source.closed
    asyncio.run(scenario())


def test_frame_returned_after_deadline_never_reaches_transport():
    from channels.models import OutputFrame, PlaybackRequest
    async def scenario():
        transport = Transport()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        deadline = time.monotonic() + .01
        async def source():
            # An external decoder can return synchronously after consuming its time budget.
            while time.monotonic() <= deadline + .001:
                pass
            yield OutputFrame("speech", session, "out", 0, b"\0\1", 16000)
        result = await channel.play(PlaybackRequest("speech", session, "out", deadline), source())
        assert result.status == "expired"
        assert transport.frames == []
    asyncio.run(scenario())


def test_cancellation_during_transport_submission_preserves_unknown_audio_extent():
    from contextlib import suppress

    from channels.models import OutputFrame, PlaybackRequest
    class PendingTransport(Transport):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
        async def push_audio(self, pcm, rate, *, finish=False):
            self.frames.append((pcm, finish))
            self.entered.set()
            await asyncio.Event().wait()
    async def scenario():
        transport = PendingTransport()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        async def source():
            yield OutputFrame("speech", session, "out", 0, b"\0\1" * 240, 24000)
        task = asyncio.create_task(channel.play(PlaybackRequest("speech", session, "out", time.monotonic()+5), source()))
        await transport.entered.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        result = channel.playback("speech")
        assert result.status == "cancelled"
        assert result.confirmed_samples == 0
        assert result.unknown_samples == 240
    asyncio.run(scenario())


def test_uncooperative_source_does_not_extend_deadline_or_allow_resource_reuse():
    from channels.models import PlaybackRequest
    class Stubborn(Source):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()
        async def __anext__(self):
            self.started = True
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()
            raise StopAsyncIteration
    async def scenario():
        channel = create_channel(Transport())
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        source = Stubborn()
        task = asyncio.create_task(channel.play(PlaybackRequest("speech", session, "out", time.monotonic()+.01), source))
        try:
            done, _ = await asyncio.wait({task}, timeout=.05)
            assert task in done, "a source ignoring cancellation must not extend the request deadline"
            assert (await task).status == "expired"
            assert source.closed
            refused = await channel.play(PlaybackRequest("new", session, "out2", time.monotonic()+1), Source())
            assert refused.reason == "cleanup_pending"
        finally:
            source.release.set()
            await task
    asyncio.run(scenario())


def test_failed_stop_blocks_following_playback_until_explicit_retirement():
    from channels.models import LeaveRoom, OutputFrame, PlaybackRequest
    class UncertainStop(Transport):
        fail = True
        async def retire_output(self):
            if self.fail:
                raise OSError("stop was not acknowledged")
            await super().retire_output()
    async def scenario():
        transport = UncertainStop()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        async def source():
            yield OutputFrame("speech", session, "out", 0, b"\0\1", 24000)
            raise RuntimeError("external source failed")
        result = await channel.play(PlaybackRequest("speech", session, "out", time.monotonic()+1), source())
        assert result.status == "partial"
        refused = await channel.play(PlaybackRequest("next", session, "out2", time.monotonic()+1), Source())
        assert refused.reason == "cleanup_pending"
        transport.fail = False
        assert (await channel.execute(LeaveRoom("leave", session))).status == "completed"
    asyncio.run(scenario())
