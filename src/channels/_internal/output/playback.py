"""单会话播放台账；真实 drain 后才发布 completed，源由执行器接管关闭。"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from channels.models import (
    AudioPushResult,
    OutputFrame,
    PlaybackRequest,
    PlaybackResult,
    PlaybackState,
    RunningAction,
    SessionRef,
    UnknownAction,
)


class PlaybackExecutor:
    def __init__(self, current: Callable[[], SessionRef | None],
                 push: Callable[..., Awaitable[AudioPushResult]],
                 drain: Callable[[float], Awaitable[AudioPushResult]],
                 stop: Callable[[], Awaitable[None]]) -> None:
        self._current, self._push, self._drain, self._stop = current, push, drain, stop
        self._tasks: dict[str, tuple[PlaybackRequest, asyncio.Task[PlaybackResult]]] = {}
        self._results: dict[str, PlaybackResult] = {}
        self._finished: dict[str, float] = {}
        self._active: asyncio.Task[PlaybackResult] | None = None
        self._sources: dict[str, AsyncIterator[OutputFrame]] = {}
        self._unstarted_cleanup: dict[str, asyncio.Task[PlaybackResult]] = {}
        self._rejections: dict[str, tuple[PlaybackRequest, PlaybackResult]] = {}
        self._cancel_requested: set[str] = set()
        self._pending_closes: set[asyncio.Future[Any]] = set()
        self._pending_work: set[asyncio.Future[Any]] = set()
        self._stop_unconfirmed = False

    def snapshot(self, action_id: str) -> PlaybackState:
        if action_id in self._rejections:
            return self._rejections[action_id][1]
        if action_id in self._results:
            return self._results[action_id]
        return RunningAction(action_id) if action_id in self._tasks else UnknownAction(action_id)

    async def _close(self, frames: AsyncIterator[OutputFrame]) -> None:
        close = getattr(frames, "aclose", None)
        if close is None:
            return
        task = asyncio.ensure_future(close())
        self._pending_closes.add(task)
        def observed(done):
            self._pending_closes.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(observed)
        deadline = time.monotonic() + 3
        cancelled = False
        while not task.done():
            try:
                done, _ = await asyncio.wait({task}, timeout=max(0, deadline-time.monotonic()))
            except asyncio.CancelledError:
                cancelled = True
                continue
            if not done:
                task.cancel()
                raise TimeoutError("source_close_failed")
        if task.cancelled():
            raise RuntimeError("source_close_failed")
        try:
            task.result()
        except Exception:
            raise RuntimeError("source_close_failed") from None
        if cancelled:
            raise asyncio.CancelledError

    async def play(self, request: PlaybackRequest,
                   frames: AsyncIterator[OutputFrame]) -> PlaybackResult:
        rejected = self._rejections.get(request.action_id)
        if rejected is not None:
            await self._close(frames)
            return rejected[1] if rejected[0] == request else PlaybackResult(
                request.action_id, request.session, request.output_id, "rejected", "action_conflict")
        previous = self._tasks.get(request.action_id)
        if previous is not None:
            if frames is not self._sources.get(request.action_id):
                await self._close(frames)
            if previous[0] != request:
                return PlaybackResult(request.action_id, request.session, request.output_id,
                                      "rejected", "action_conflict")
            if request.action_id in self._results:
                return self._results[request.action_id]
            try:
                return await asyncio.shield(previous[1])
            except asyncio.CancelledError:
                if previous[1].cancelled():
                    return await self._finish_unstarted(request.action_id)
                raise
        now = time.monotonic()
        for key, finished in list(self._finished.items()):
            if now - finished >= 600:
                del self._tasks[key], self._results[key], self._finished[key]
                self._unstarted_cleanup.pop(key, None)
                self._cancel_requested.discard(key)
        reason = ("busy" if self._active is not None and not self._active.done()
                  else "cleanup_pending" if self._stop_unconfirmed or any(not task.done() for task in self._pending_closes | self._pending_work)
                  else "capacity" if len(self._tasks) >= 1024
                  else "stale_session" if self._current() != request.session else "")
        if reason or request.deadline <= now:
            result = PlaybackResult(request.action_id, request.session, request.output_id,
                                    "rejected" if reason else "expired", reason or "deadline")
            self._rejections[request.action_id] = request, result
            if len(self._rejections) > 128:
                self._rejections.pop(next(iter(self._rejections)))
            try:
                await self._close(frames)
            except asyncio.CancelledError:
                raise
            except Exception:
                result = PlaybackResult(request.action_id, request.session, request.output_id,
                                        "rejected", "source_close_failed")
                self._rejections[request.action_id] = request, result
            return result
        task = asyncio.create_task(self._run(request, frames), name="channel-playback")
        self._active = task
        self._tasks[request.action_id] = request, task
        self._sources[request.action_id] = frames
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            owner_cancelled = not task.cancelled()
            await self._cancel_task(request.action_id)
            if owner_cancelled:
                raise
            return self._results[request.action_id]

    async def _finish_unstarted(self, action_id: str) -> PlaybackResult:
        if action_id in self._results:
            return self._results[action_id]
        cleanup = self._unstarted_cleanup.get(action_id)
        if cleanup is None:
            request = self._tasks[action_id][0]
            source = self._sources[action_id]
            async def finish():
                reason = "not_started"
                try:
                    await self._close(source)
                except BaseException:
                    reason = "source_close_failed"
                result = PlaybackResult(request.action_id, request.session, request.output_id,
                                        "cancelled", reason)
                self._results[action_id] = result
                self._finished[action_id] = time.monotonic()
                self._sources.pop(action_id, None)
                return result
            cleanup = asyncio.create_task(finish(), name="channel-playback-cleanup")
            self._unstarted_cleanup[action_id] = cleanup
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        return cleanup.result()

    async def _cancel_task(self, action_id: str) -> PlaybackResult:
        task = self._tasks[action_id][1]
        if not task.done() and action_id not in self._cancel_requested:
            self._cancel_requested.add(action_id)
            task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if task.cancelled():
            return await self._finish_unstarted(action_id)
        return task.result()

    async def retire(self) -> None:
        task = self._active
        if task is not None and not task.done():
            action_id = next(key for key, value in self._tasks.items() if value[1] is task)
            await self._cancel_task(action_id)
        if self._stop_unconfirmed:
            await self._bounded(self._stop(), 3)
            self._stop_unconfirmed = False
        if any(not pending.done() for pending in self._pending_closes | self._pending_work):
            raise TimeoutError("source_cleanup_pending")

    async def cancel(self, action_id: str) -> PlaybackState:
        entry = self._tasks.get(action_id)
        if entry is not None and not entry[1].done():
            await self._cancel_task(action_id)
        elif entry is not None and entry[1].cancelled():
            await self._finish_unstarted(action_id)
        return self.snapshot(action_id)

    async def _bounded(self, work, timeout):
        task = asyncio.ensure_future(work)
        self._pending_work.add(task)
        def observed(done):
            self._pending_work.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(observed)
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0, timeout))
            if not done:
                task.cancel()
                raise TimeoutError
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _run(self, request: PlaybackRequest,
                   frames: AsyncIterator[OutputFrame]) -> PlaybackResult:
        count = samples = confirmed = rate = 0
        status: Any = "failed"
        reason = "source_failed"
        async def bounded(work):
            return await self._bounded(work, min(10, max(0, request.deadline - time.monotonic())))
        try:
            iterator = frames.__aiter__()
            while True:
                if request.deadline <= time.monotonic():
                    raise TimeoutError
                if self._current() != request.session:
                    raise ValueError("stale_session")
                try:
                    frame = await bounded(iterator.__anext__())
                except StopAsyncIteration:
                    break
                if (not isinstance(frame, OutputFrame) or frame.action_id != request.action_id
                        or frame.session != request.session or frame.output_id != request.output_id
                        or frame.frame_seq != count or (rate and frame.sample_rate != rate)):
                    raise ValueError("frame_contract")
                if request.deadline <= time.monotonic():
                    raise TimeoutError
                if self._current() != request.session:
                    raise ValueError("stale_session")
                rate = frame.sample_rate
                # Submission can have external effects even if its acknowledgement is cancelled.
                count += 1
                samples += len(frame.pcm16) // 2
                pushed = await bounded(self._push(request.session, frame.pcm16, rate))
                if not pushed.ok:
                    if pushed.reason not in {"transport_failed", "effects_unconfirmed"}:
                        count -= 1
                        samples -= len(frame.pcm16) // 2
                    raise ValueError(pushed.reason or "push_failed")
            if not count:
                raise ValueError("empty_output")
            finished = await bounded(self._push(request.session, b"", rate, finish=True))
            if not finished.ok:
                raise ValueError(finished.reason or "finish_failed")
            drain_budget = min(30, max(0, request.deadline - time.monotonic()))
            drained = await self._bounded(self._drain(drain_budget), drain_budget)
            if not drained.ok or self._current() != request.session:
                raise ValueError(drained.reason or "drain_failed")
            if request.deadline <= time.monotonic():
                raise TimeoutError
            confirmed = samples
            status, reason = "completed", ""
        except asyncio.CancelledError:
            status, reason = "cancelled", "cancelled"
        except (TimeoutError, asyncio.TimeoutError):
            status, reason = "partial" if samples else "expired", "deadline"
        except Exception as exc:
            status = "partial" if samples else "failed"
            # 只保留本地错误码，源异常可能包含模型正文或凭据。
            reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {"stale_session", "frame_contract", "push_failed", "empty_output", "finish_failed", "drain_failed", "confirmation_unavailable", "transport_failed", "output_muted"} else "source_failed"
        finally:
            if status != "completed":
                try:
                    await self._bounded(self._stop(), 3)
                except (Exception, asyncio.CancelledError):
                    self._stop_unconfirmed = True
            try:
                await self._close(frames)
            except Exception:
                status, reason = "partial" if samples else "failed", "source_close_failed"
            result = PlaybackResult(request.action_id, request.session, request.output_id,
                                    status, reason, count, samples, confirmed, samples - confirmed)
            self._results[request.action_id] = result
            self._finished[request.action_id] = time.monotonic()
            self._sources.pop(request.action_id, None)
        return result
