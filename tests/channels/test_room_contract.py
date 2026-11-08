"""通过渠道公开边界验证真实房间状态；仅替换外部传输。"""

import asyncio
from contextlib import suppress

from channels import create_channel
from channels.models import AudioPushResult, JoinRoom, RoomState, RoomTarget


class Transport:
    async def join(self, target: RoomTarget) -> str:
        return "rtc-test"

    async def push_audio(self, pcm: bytes, rate: int, *, finish: bool = False) -> AudioPushResult:
        return AudioPushResult(True)

    async def leave(self) -> None:
        pass

    async def retire_output(self) -> None:
        pass


def test_confirmed_join_is_queryable_by_session_and_action() -> None:
    async def run() -> None:
        channel = create_channel(Transport())
        command = JoinRoom("join-1", RoomTarget("a", "c"), expected_revision=0)
        result = await channel.execute(command)
        assert result.status == "completed"
        view = channel.snapshot()
        assert view.state is RoomState.JOINED
        assert view.session is not None and view.session.room_target == RoomTarget("a", "c")
        assert channel.outcome("join-1") == result

    asyncio.run(run())


def test_failed_leave_preserves_uncertainty_and_can_be_retried() -> None:
    from channels.models import LeaveRoom

    class FailingTransport(Transport):
        fail = True

        async def leave(self) -> None:
            if self.fail:
                raise OSError("external secret must not enter the result")

    async def run() -> None:
        transport = FailingTransport()
        channel = create_channel(transport)
        joined = await channel.execute(JoinRoom("join", RoomTarget("a", "c"), 0))
        assert joined.session is not None
        result = await channel.execute(LeaveRoom("leave", joined.session))
        assert result.status == "failed" and result.reason == "transport_failed"
        assert channel.snapshot().state is RoomState.UNCERTAIN
        assert channel.snapshot().session == joined.session
        assert channel.outcome("leave") == result
        transport.fail = False
        assert (await channel.execute(LeaveRoom("retry", joined.session))).status == "completed"
        assert channel.snapshot().state is RoomState.IDLE

    asyncio.run(run())


def test_old_session_cannot_leave_or_replace_the_new_room() -> None:
    from channels.models import LeaveRoom

    async def run() -> None:
        channel = create_channel(Transport())
        first = await channel.execute(JoinRoom("first", RoomTarget("a", "c"), 0))
        second = await channel.execute(
            JoinRoom("second", RoomTarget("a", "d"), channel.snapshot().revision)
        )
        assert first.session is not None and second.session != first.session
        before = channel.snapshot()
        stale = await channel.execute(LeaveRoom("old-leave", first.session))
        assert stale.status == "rejected" and stale.reason == "stale_session"
        assert channel.snapshot() == before
        conflict = await channel.execute(JoinRoom("old-join", RoomTarget("b", "e"), 0))
        assert conflict.status == "rejected" and conflict.reason == "stale_revision"
        assert channel.snapshot() == before

    asyncio.run(run())


def test_duplicate_join_waits_for_original_and_conflicting_id_is_rejected() -> None:
    class BlockedTransport(Transport):
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.rooms: list[RoomTarget] = []

        async def join(self, target: RoomTarget) -> str:
            self.entered.set()
            await self.release.wait()
            self.rooms.append(target)
            return "rtc-test"

    async def run() -> None:
        transport = BlockedTransport()
        channel = create_channel(transport)
        command = JoinRoom("same", RoomTarget("a", "c"), 0)
        original = asyncio.create_task(channel.execute(command))
        await asyncio.wait_for(transport.entered.wait(), 1)
        retry = asyncio.create_task(channel.execute(command))
        await asyncio.sleep(0)
        retry.cancel()
        with suppress(asyncio.CancelledError):
            await retry
        transport.release.set()
        result = await original
        assert await channel.execute(command) == result
        assert transport.rooms == [RoomTarget("a", "c")]
        conflict = await channel.execute(JoinRoom("same", RoomTarget("a", "different"), 0))
        assert conflict.status == "rejected" and conflict.reason == "action_conflict"
        assert channel.outcome("same") == result

    asyncio.run(run())


def test_cancelled_join_cleans_remote_room_before_next_join() -> None:
    class CancelledTransport(Transport):
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.current: RoomTarget | None = None

        async def join(self, target: RoomTarget) -> str:
            self.current = target  # 远端已生效，本地还没拿到确认。
            self.entered.set()
            await self.release.wait()
            return "rtc-test"

        async def leave(self) -> None:
            self.current = None

    async def run() -> None:
        transport = CancelledTransport()
        channel = create_channel(transport)
        task = asyncio.create_task(channel.execute(JoinRoom("cancel-me", RoomTarget("a", "c"), 0)))
        await asyncio.wait_for(transport.entered.wait(), 1)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert transport.current is None
        result = channel.outcome("cancel-me")
        assert result is not None and result.status == "cancelled"
        assert channel.snapshot().state is RoomState.IDLE
        repeated = await channel.execute(JoinRoom("cancel-me", RoomTarget("a", "c"), 0))
        assert repeated == result

    asyncio.run(run())


def test_join_failure_is_queryable_and_blocks_automatic_replacement() -> None:
    class BrokenTransport(Transport):
        async def join(self, target: RoomTarget) -> str:
            raise OSError("remote join effect unknown")

    async def run() -> None:
        channel = create_channel(BrokenTransport())
        failed = await channel.execute(JoinRoom("failed", RoomTarget("a", "c"), 0))
        assert failed.status == "failed" and channel.outcome("failed") == failed
        assert channel.snapshot().state is RoomState.UNCERTAIN
        result = await channel.execute(
            JoinRoom("unsafe", RoomTarget("a", "d"), channel.snapshot().revision, "auto")
        )
        assert result.status == "rejected" and result.reason == "room_uncertain"

    asyncio.run(run())


def test_switch_waits_for_inflight_audio_and_rejects_old_session_frames() -> None:
    class AudioTransport(Transport):
        def __init__(self) -> None:
            self.pushing = asyncio.Event()
            self.release = asyncio.Event()
            self.room: RoomTarget | None = None
            self.heard: list[tuple[RoomTarget | None, bytes]] = []

        async def join(self, target: RoomTarget) -> str:
            self.room = target
            return "rtc-test"

        async def push_audio(
            self, pcm: bytes, rate: int, *, finish: bool = False
        ) -> AudioPushResult:
            self.pushing.set()
            await self.release.wait()
            self.heard.append((self.room, pcm))
            return AudioPushResult(True)

    async def run() -> None:
        transport = AudioTransport()
        channel = create_channel(transport)
        first = await channel.execute(JoinRoom("first", RoomTarget("a", "old"), 0))
        assert first.session is not None
        push = asyncio.create_task(channel.push_audio(first.session, b"\x00\x01", 24000))
        await asyncio.wait_for(transport.pushing.wait(), 1)
        switch = asyncio.create_task(
            channel.execute(JoinRoom("switch", RoomTarget("a", "new"), channel.snapshot().revision))
        )
        done, _ = await asyncio.wait({switch}, timeout=0.02)
        assert not done, "在途音频未收尾时，切房不能完成"
        assert transport.room == RoomTarget("a", "old")
        transport.release.set()
        await push
        assert (await switch).status == "completed"
        assert transport.heard == [(RoomTarget("a", "old"), b"\x00\x01")]
        assert not (await channel.push_audio(first.session, b"\x02\x03", 24000)).ok
        assert len(transport.heard) == 1

    asyncio.run(run())


def test_existing_voice_entry_uses_channel_room_state(tmp_path) -> None:
    from support.channel_fixtures import make_voice_agent as make_agent

    async def run() -> None:
        agent = make_agent(tmp_path)
        await agent.join("a", "c")
        session = agent.channels.snapshot().session
        assert session is not None and session.room_target == RoomTarget("a", "c")
        assert agent.status()["joined"]
        await agent.leave()
        assert agent.channels.snapshot().state is RoomState.IDLE
        assert not agent.status()["joined"]

    asyncio.run(run())


def test_cancelling_a_queued_action_does_not_leave_the_active_transition() -> None:
    class BlockingTransport(Transport):
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def join(self, target: RoomTarget) -> str:
            self.entered.set()
            await self.release.wait()
            return "rtc-test"

    async def run() -> None:
        transport = BlockingTransport()
        channel = create_channel(transport)
        first = asyncio.create_task(channel.execute(JoinRoom("first", RoomTarget("a", "c"), 0)))
        await asyncio.wait_for(transport.entered.wait(), 1)
        queued = asyncio.create_task(channel.execute(JoinRoom("queued", RoomTarget("a", "d"), 0)))
        await asyncio.sleep(0)
        queued.cancel()
        with suppress(asyncio.CancelledError):
            await queued
        result = channel.outcome("queued")
        assert result is not None and result.status == "cancelled"
        transport.release.set()
        await first
        assert channel.snapshot().confirmed
        assert channel.snapshot().session.room_target == RoomTarget("a", "c")

    asyncio.run(run())


def test_recent_results_are_retained_and_capacity_rejects_new_actions() -> None:
    async def run() -> None:
        channel = create_channel(Transport())
        first = None
        for index in range(1024):
            result = await channel.execute(
                JoinRoom(str(index), RoomTarget("a", "c"), channel.snapshot().revision)
            )
            assert result.status == "completed"
            if index == 0:
                first = result
        result = await channel.execute(
            JoinRoom("overflow", RoomTarget("a", "c"), channel.snapshot().revision)
        )
        assert result.status == "rejected" and result.reason == "busy"
        assert channel.outcome("0") == first

    asyncio.run(run())


def test_invalid_command_is_rejected_before_any_room_io() -> None:
    import pytest

    for action_id, revision, source in [
        ("", 0, "manual"),
        ("a", True, "manual"),
        ("a", -1, "manual"),
        ("a", 0, "unknown"),
    ]:
        with pytest.raises(ValueError):
            JoinRoom(action_id, RoomTarget("a", "c"), revision, source)


def test_old_bot_join_cannot_activate_a_new_binding(tmp_path) -> None:
    import pytest
    from support.channel_fixtures import make_voice_agent as make_agent

    async def run() -> None:
        agent = make_agent(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        old_voice = agent._bot.voice

        async def join(**kwargs):
            entered.set()
            await release.wait()
            return object()

        old_voice.join = join
        task = asyncio.create_task(agent.join("a", "old"))
        await asyncio.wait_for(entered.wait(), 1)
        replacement = make_agent(tmp_path / "other")
        agent.bind_bot(replacement._bot)
        release.set()
        with pytest.raises(RuntimeError, match="superseded"):
            await task
        assert not agent.status()["joined"]
        assert old_voice.leaves == 1
        assert agent._round_task is None

    asyncio.run(run())


def test_running_action_and_unknown_action_are_distinguishable() -> None:
    class BlockedTransport(Transport):
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def join(self, target: RoomTarget) -> str:
            self.entered.set()
            await self.release.wait()
            return "rtc-test"

    async def run() -> None:
        transport = BlockedTransport()
        channel = create_channel(transport)
        task = asyncio.create_task(channel.execute(JoinRoom("pending", RoomTarget("a", "c"), 0)))
        await asyncio.wait_for(transport.entered.wait(), 1)
        try:
            assert channel.outcome("pending").status == "running"
            assert channel.outcome("missing").status == "unknown"
        finally:
            transport.release.set()
            await task

    asyncio.run(run())


def test_uncooperative_audio_timeout_blocks_transport_reuse() -> None:
    class StuckTransport(Transport):
        def __init__(self) -> None:
            self.release = asyncio.Event()

        async def push_audio(
            self, pcm: bytes, rate: int, *, finish: bool = False
        ) -> AudioPushResult:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
            return AudioPushResult(True)

    async def run() -> None:
        transport = StuckTransport()
        channel = create_channel(transport)
        result = await channel.execute(JoinRoom("first", RoomTarget("a", "c"), 0))
        task = asyncio.create_task(channel.push_audio(result.session, b"\x00\x01", 24000))
        try:
            done, _ = await asyncio.wait({task}, timeout=4)
            assert done, "外部传输忽略取消时，公开调用仍须在预算内返回"
            assert not task.result().ok
            assert channel.snapshot().state is RoomState.UNCERTAIN
            next_result = await channel.execute(
                JoinRoom("unsafe", RoomTarget("a", "new"), channel.snapshot().revision)
            )
            assert next_result.reason == "room_uncertain"
        finally:
            transport.release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_rebinding_cannot_reuse_transport_before_old_push_drains(tmp_path) -> None:
    from types import SimpleNamespace

    from support.channel_fixtures import make_voice_agent

    async def run() -> None:
        agent = make_voice_agent(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        class Output:
            transport = None

            async def enable_listen(self, enabled=True):
                return {"ok": True}

            async def stop_tts(self):
                return {"ok": True}

            async def push_tts_pcm(self, pcm, rate, *, finish=False):
                entered.set()
                await release.wait()
                return {"ok": True}

        agent.duplex = Output()
        await agent.join("a", "old")
        old_channel = agent.channels
        push = asyncio.create_task(
            old_channel.push_audio(old_channel.snapshot().session, b"\x00\x01", 24000)
        )
        await asyncio.wait_for(entered.wait(), 1)
        # 不同 SDK 对象可能仍包着同一个物理 transport，不能仅换 ID 就复用。
        replacement = SimpleNamespace(voice=agent._bot.voice)
        agent.bind_bot(replacement)
        switch = asyncio.create_task(agent.join("a", "new"))
        try:
            done, _ = await asyncio.wait({switch}, timeout=0.02)
            assert not done
        finally:
            release.set()
            await push
            await switch
            await agent.leave()

    asyncio.run(run())


def test_programming_error_is_not_disguised_as_a_transport_failure() -> None:
    import pytest

    class BrokenAdapter(Transport):
        async def join(self, target: RoomTarget) -> str:
            raise TypeError("adapter implementation bug")

    async def run() -> None:
        channel = create_channel(BrokenAdapter())
        with pytest.raises(TypeError, match="adapter implementation bug"):
            await channel.execute(JoinRoom("bug", RoomTarget("a", "c"), 0))
        assert channel.snapshot().state is RoomState.UNCERTAIN
        assert channel.outcome("bug").reason == "internal_error"

    asyncio.run(run())
