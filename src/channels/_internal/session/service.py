"""实际房间状态的唯一拥有者。"""

import asyncio
import time
from collections.abc import Awaitable
from typing import Any, TypeVar
from uuid import uuid4

from channels._internal.output.playback import PlaybackExecutor
from channels.api import RoomTransport, TransportFailure
from channels.models import (
    ActionOutcome,
    ActionState,
    AudioPushResult,
    ChannelCommand,
    ChannelSnapshot,
    LeaveRoom,
    RoomState,
    RunningAction,
    ScreenCapture,
    SendText,
    SessionRef,
    SetOutput,
    UnknownAction,
)

T = TypeVar("T")


class RoomChannel:
    def __init__(self, transport: RoomTransport) -> None:
        self._lock = asyncio.Lock()
        self._output_lock = asyncio.Lock()
        self._capture_lock = asyncio.Lock()
        self._capture_task = None
        self._pending_io: set[asyncio.Future[Any]] = set()
        self._transport = transport
        self._run_id = uuid4().hex
        self._binding_id = uuid4().hex
        self._view = ChannelSnapshot(0, RoomState.IDLE)
        self._output_uncertain_session: SessionRef | None = None
        self._text_started: set[str] = set()
        self._playback = PlaybackExecutor(
            lambda: self._view.session if self._view.state is RoomState.JOINED else None,
            self.push_audio, self._wait_playback, self._stop_playback)
        self._results: dict[str, ActionOutcome] = {}
        self._finished: dict[str, float] = {}
        self._tasks: dict[str, tuple[ChannelCommand, asyncio.Task[ActionOutcome]]] = {}

    async def capture_screen(self, session):
        if self._view.state is not RoomState.JOINED or self._view.session != session:
            return ScreenCapture("stale_session")
        capture = getattr(self._transport, "capture_screen", None)
        if capture is None:
            return ScreenCapture("unavailable")
        if self._capture_lock.locked():
            return ScreenCapture("unavailable")
        async with self._capture_lock:
            work = asyncio.create_task(capture(session))
            self._capture_task = work
            try:
                result = await asyncio.wait_for(asyncio.shield(work), 5)
            except asyncio.CancelledError:
                if work.cancelled() and (self._view.state is not RoomState.JOINED or self._view.session != session):
                    return ScreenCapture("stale_session")
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                raise
            except Exception:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                return ScreenCapture("unavailable")
            finally:
                if self._capture_task is work:
                    self._capture_task = None
        if self._view.state is not RoomState.JOINED or self._view.session != session:
            return ScreenCapture("stale_session")
        if not isinstance(result, ScreenCapture):
            return ScreenCapture("unavailable")
        if result.frame is not None and (result.frame.session != session
                or not 0 <= time.monotonic() - result.frame.captured_at <= 3):
            return ScreenCapture("stale_session")
        return result

    async def play(self, request, frames):
        return await self._playback.play(request, frames)

    def playback(self, action_id):
        return self._playback.snapshot(action_id)

    async def cancel_playback(self, action_id):
        return await self._playback.cancel(action_id)

    async def _stop_playback(self) -> None:
        # A browser may accept its in-flight push while cancellation is being observed.
        # Drain that exact work before clearing the queue, otherwise late PCM follows stop.
        async with self._output_lock:
            if self._pending_io:
                _, pending = await asyncio.wait(self._pending_io, timeout=2)
                if pending:
                    raise TimeoutError("旧推送尚未隔离")
            await self._io(self._transport.retire_output(), 2)
            if (self._view.state is RoomState.UNCERTAIN and self._view.confirmed
                    and self._view.session == self._output_uncertain_session):
                self._view = ChannelSnapshot(self._view.revision + 1, RoomState.JOINED,
                                             self._view.session, True)
                self._output_uncertain_session = None

    async def _wait_playback(self, timeout):
        drain = getattr(self._transport, "wait_playback", None)
        if drain is None:
            return AudioPushResult(False, "confirmation_unavailable")
        return await self._io(drain(timeout), timeout)

    def snapshot(self) -> ChannelSnapshot:
        return self._view

    def outcome(self, action_id: str) -> ActionState:
        entry = self._tasks.get(action_id)
        if entry is not None and not entry[1].done():
            return RunningAction(action_id)
        return self._results.get(action_id, UnknownAction(action_id))

    async def execute(self, command: ChannelCommand) -> ActionOutcome:
        entry = self._tasks.get(command.action_id)
        if entry is not None:
            previous, task = entry
            if previous != command:
                return ActionOutcome(command.action_id, "rejected", reason="action_conflict")
            if task.done() and command.action_id in self._results:
                return self._results[command.action_id]
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled() and command.action_id in self._results:
                    return self._results[command.action_id]
                raise
        now = time.monotonic()
        for action_id, ended in list(self._finished.items()):
            if now - ended >= 600 and self._tasks[action_id][1].done():
                del self._tasks[action_id], self._results[action_id], self._finished[action_id]
                self._text_started.discard(action_id)
        if (
            len(self._tasks) >= 1024
            or sum(not task.done() for _, task in self._tasks.values()) >= 128
        ):
            return ActionOutcome(command.action_id, "rejected", reason="busy")
        task = asyncio.create_task(self._serialized(command), name="channel-room-action")
        self._tasks[command.action_id] = (command, task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            if command.action_id not in self._results:
                self._remember(ActionOutcome(command.action_id, "cancelled", reason="not_started"))
            raise

    async def _serialized(self, command: ChannelCommand) -> ActionOutcome:
        async with self._lock:
            try:
                return await self._execute(command)
            except asyncio.CancelledError:
                if isinstance(command, (SetOutput, SendText)):
                    self._remember(ActionOutcome(command.action_id, "cancelled", command.session,
                        "not_started" if isinstance(command, SendText) and command.action_id not in self._text_started else "effects_unconfirmed",
                        media="text" if isinstance(command, SendText) else "output",
                        target=command.target if isinstance(command, SendText) else None,
                        parent_action_id=command.parent_action_id if isinstance(command, SendText) else ""))
                    raise
                session = self._view.session
                try:
                    await self._retire()
                    await self._io(self._transport.leave(), 3)
                except Exception:
                    self._view = ChannelSnapshot(
                        self._view.revision + 1, RoomState.UNCERTAIN, session, self._view.confirmed
                    )
                else:
                    self._view = ChannelSnapshot(self._view.revision + 1, RoomState.IDLE)
                self._remember(ActionOutcome(command.action_id, "cancelled", session, "cancelled"))
                raise
            except Exception:
                if command.action_id not in self._results:
                    self._remember(ActionOutcome(command.action_id, "failed", self._view.session, "internal_error"))
                raise

    async def _execute(self, command: ChannelCommand) -> ActionOutcome:
        if isinstance(command, (LeaveRoom, SetOutput, SendText)) and command.session != self._view.session:
            return self._remember(
                ActionOutcome(command.action_id, "rejected", command.session, "stale_session",
                    media="text" if isinstance(command, SendText) else None,
                    target=command.target if isinstance(command, SendText) else None,
                    parent_action_id=command.parent_action_id if isinstance(command, SendText) else "")
            )
        if isinstance(command, SendText):
            send = getattr(self._transport, "send_text", None)
            def outcome(status, reason="", message_id=""):
                return self._remember(ActionOutcome(command.action_id, status, command.session, reason,
                    media="text", message_id=message_id, target=command.target,
                    parent_action_id=command.parent_action_id))
            if self._view.state is not RoomState.JOINED or send is None:
                return outcome("rejected", "text_unavailable")
            if any(not work.done() for work in self._pending_io):
                return outcome("rejected", "effects_pending")
            validate_target = getattr(self._transport, "text_target_exists", None)
            if validate_target is None:
                return outcome("rejected", "target_unavailable")
            try:
                valid = await self._io(validate_target(command.target), 10)
            except (TransportFailure, OSError, TimeoutError):
                return outcome("rejected", "target_unavailable")
            if valid is not True:
                return outcome("rejected", "target_unavailable")
            self._text_started.add(command.action_id)
            try:
                message_id = await self._io(send(command.target, command.text), 15)
            except (TransportFailure, OSError, TimeoutError):
                return outcome("failed", "effects_unconfirmed")
            if not isinstance(message_id, str) or not message_id.strip():
                return outcome("failed", "effects_unconfirmed")
            return outcome("completed", message_id=message_id)
        if isinstance(command, SetOutput):
            apply = getattr(self._transport, "apply_output", None)
            if self._view.state is not RoomState.JOINED or apply is None:
                return self._remember(ActionOutcome(command.action_id, "rejected", command.session, "output_unavailable"))
            async with self._output_lock:
                if any(not task.done() for task in self._pending_io):
                    return self._remember(ActionOutcome(command.action_id, "rejected", command.session, "output_busy"))
                try:
                    applied = await self._io(apply(command.volume, command.muted), 10)
                except (TransportFailure, OSError, TimeoutError):
                    return self._remember(ActionOutcome(command.action_id, "failed", command.session, "transport_failed"))
                return self._remember(ActionOutcome(command.action_id,
                    "completed" if applied.ok else "failed", command.session, applied.reason))
        if not isinstance(command, LeaveRoom) and command.expected_revision != self._view.revision:
            return self._remember(
                ActionOutcome(command.action_id, "rejected", reason="stale_revision")
            )
        if isinstance(command, LeaveRoom):
            session = self._view.session
            try:
                await self._retire()
                await self._io(self._transport.leave(), 10)
            except Exception as exc:
                self._view = ChannelSnapshot(
                    self._view.revision + 1, RoomState.UNCERTAIN, session, self._view.confirmed
                )
                if not isinstance(exc, (TransportFailure, OSError, TimeoutError)):
                    self._remember(
                        ActionOutcome(command.action_id, "failed", session, "internal_error")
                    )
                    raise
                result = ActionOutcome(command.action_id, "failed", session, "transport_failed")
            else:
                self._view = ChannelSnapshot(self._view.revision + 1, RoomState.IDLE)
                result = ActionOutcome(command.action_id, "completed", session)
            return self._remember(result)
        if self._view.state is RoomState.UNCERTAIN:
            return self._remember(
                ActionOutcome(command.action_id, "rejected", reason="room_uncertain")
            )
        if self._view.session is not None and command.source == "auto":
            return self._remember(
                ActionOutcome(command.action_id, "rejected", reason="room_occupied")
            )
        if self._view.session is not None:
            left = await self._execute(
                LeaveRoom(command.action_id, self._view.session, command.source)
            )
            if left.status != "completed":
                return left
        session = SessionRef(self._run_id, self._binding_id, uuid4().hex, command.target)
        self._view = ChannelSnapshot(self._view.revision + 1, RoomState.JOINING, session)
        try:
            rtc = await self._io(self._transport.join(command.target), 30)
        except Exception as exc:
            self._view = ChannelSnapshot(
                self._view.revision + 1, RoomState.UNCERTAIN, session, self._view.confirmed
            )
            if not isinstance(exc, (TransportFailure, OSError, TimeoutError)):
                self._remember(
                    ActionOutcome(command.action_id, "failed", session, "internal_error")
                )
                raise
            return self._remember(
                ActionOutcome(command.action_id, "failed", session, "transport_failed")
            )
        self._view = ChannelSnapshot(self._view.revision + 1, RoomState.JOINED, session, True)
        result = ActionOutcome(command.action_id, "completed", session, rtc_channel=rtc)
        return self._remember(result)

    async def _retire(self) -> None:
        self._view = ChannelSnapshot(
            self._view.revision + 1, RoomState.RETIRING, self._view.session, self._view.confirmed
        )

        capture = self._capture_task
        if capture is not None:
            capture.cancel()
            await asyncio.wait_for(asyncio.gather(capture, return_exceptions=True), 1)
        await self._playback.retire()

        async def drain() -> None:
            async with self._output_lock:
                if self._pending_io:
                    _, pending = await asyncio.wait(self._pending_io, timeout=2)
                    if pending:
                        raise TimeoutError("旧传输尚未隔离，禁止复用")
                await self._io(self._transport.retire_output(), 2)

        await asyncio.wait_for(drain(), 3)

    async def push_audio(
        self, session: SessionRef, pcm: bytes, rate: int, *, finish: bool = False
    ) -> AudioPushResult:
        async with self._output_lock:
            if self._view.state is not RoomState.JOINED or self._view.session != session:
                return AudioPushResult(False, "stale_session")
            try:
                return await self._io(self._transport.push_audio(pcm, rate, finish=finish), 3)
            except (Exception, asyncio.CancelledError) as exc:
                self._output_uncertain_session = session
                self._view = ChannelSnapshot(
                    self._view.revision + 1, RoomState.UNCERTAIN, session, self._view.confirmed
                )
                if not isinstance(exc, (TransportFailure, OSError, TimeoutError)):
                    raise
                return AudioPushResult(False, "transport_failed")

    async def _io(self, work: Awaitable[T], timeout: float) -> T:
        # wait_for 会等待被取消的外部协程，外部忽略取消时可能超出预算。
        # 保留未完成引用供退房屏障核对；未知效果期间不复用同一传输。
        task = asyncio.ensure_future(work)
        self._pending_io.add(task)

        def observed(done: asyncio.Future[Any]) -> None:
            self._pending_io.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(observed)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                task.cancel()
                raise TimeoutError("外部传输超时")
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise

    def _remember(self, result: ActionOutcome) -> ActionOutcome:
        self._results[result.action_id] = result
        self._finished[result.action_id] = time.monotonic()
        return result
