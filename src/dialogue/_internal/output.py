"""单订阅表达任务及生成流到渠道帧的投影，不负责播放。"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from typing import Literal
from uuid import uuid4

from channels.models import OutputFrame, OutputSourceError, SessionRef
from dialogue.api import ExpressionJob as JobContract
from dialogue.models import GenerationEvent, GenerationEvidence

Emit = Callable[..., Awaitable[None]]
Producer = Callable[[Emit], Awaitable[None]]


class ExpressionJob:
    """有界生产任务，事件仅能订阅一次，关闭等待供应商收尾。

    每任务最多缓存 queue_size 个64KiB音频事件；绝对截止也限制背压等待。
    生产者异常转为具名失败，日志/证据不包含外部异常正文。
    """

    def __init__(self, action_id: str, session: SessionRef, producer: Producer, *,
                 deadline: float, queue_size: int = 16) -> None:
        if not action_id or queue_size < 1 or deadline <= time.monotonic():
            raise ValueError("表达任务须有动作、有限容量及未来期限")
        self.generation_id = uuid4().hex
        self.action_id, self.session = action_id, session
        self._queue: asyncio.Queue[GenerationEvent] = asyncio.Queue(queue_size)
        self._claimed = False
        self._seq = 0
        self._deadline = deadline
        self._closed = asyncio.Event()
        self._evidence = GenerationEvidence(self.generation_id, action_id, session, "running")
        self._task = asyncio.create_task(self._run(producer), name="dialogue-expression")

    async def _emit(self, kind: Literal["text_delta", "text_final", "audio", "timing", "completed", "failed"], *, text: str = "", pcm16: bytes = b"",
                    sample_rate: int = 24000, reason: str = "",
                    timings_ms: tuple[tuple[str, float], ...] = ()) -> None:
        if kind not in {"text_delta", "text_final", "audio", "timing"}:
            raise ValueError("未知生成事件")
        if kind == "audio":
            if (not pcm16 or len(pcm16) % 2 or type(sample_rate) is not int
                    or not 8000 <= sample_rate <= 96000):
                raise ValueError("供应商音频格式非法")
            for offset in range(0, len(pcm16), 65536):
                await self._enqueue(kind, text="", pcm16=pcm16[offset:offset + 65536],
                                    sample_rate=sample_rate)
        else:
            await self._enqueue(kind, text=text, reason=reason, timings_ms=timings_ms)

    async def _enqueue(self, kind: Literal["text_delta", "text_final", "audio", "timing", "completed", "failed"], **values) -> None:
        if self._closed.is_set():
            raise asyncio.CancelledError
        event = GenerationEvent(self.generation_id, self.action_id, self.session,
                                self._seq, kind, **values)
        await self._queue.put(event)
        self._seq += 1

    async def _run(self, producer: Producer) -> None:
        started = time.monotonic()
        try:
            async def generate() -> None:
                await producer(self._emit)
                await self._enqueue("completed")
            await asyncio.wait_for(generate(), max(0, self._deadline - time.monotonic()))
            self._evidence = replace(self._evidence, status="completed")
        except asyncio.CancelledError:
            self._evidence = replace(self._evidence, status="cancelled", reason="cancelled")
            raise
        except Exception as exc:
            reason = "deadline" if isinstance(exc, asyncio.TimeoutError) else "provider_failed"
            self._evidence = replace(self._evidence, status="failed", reason=reason)
            # 失败队列已满时消费者将先读完部分输出，再通过snapshot得到失败；
            # 不再阻塞资源收尾，也不把缺终态视作正常EOS。
            if not self._queue.full():
                self._queue.put_nowait(GenerationEvent(self.generation_id, self.action_id,
                    self.session, self._seq, "failed", reason=reason))
        finally:
            self._evidence = replace(self._evidence, timings_ms=(("generation_total", round((time.monotonic()-started)*1000, 1)),))
            self._closed.set()

    async def events(self) -> AsyncIterator[GenerationEvent]:
        if self._claimed:
            raise RuntimeError("generation events allow one consumer")
        self._claimed = True
        try:
            while True:
                if not self._queue.empty():
                    event = self._queue.get_nowait()
                elif self._closed.is_set():
                    break
                else:
                    getter = asyncio.create_task(self._queue.get())
                    done = asyncio.create_task(self._closed.wait())
                    try:
                        await asyncio.wait({getter, done}, return_when=asyncio.FIRST_COMPLETED)
                        if getter.done():
                            event = getter.result()
                        else:
                            continue
                    finally:
                        for task in (getter, done):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(getter, done, return_exceptions=True)
                yield event
                if event.kind in {"completed", "failed"}:
                    break
        finally:
            await self.aclose()

    def snapshot(self) -> GenerationEvidence:
        return self._evidence

    async def aclose(self) -> None:
        if not self._task.done():
            self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._closed.set()
        if self._evidence.status == "running":
            self._evidence = replace(self._evidence, status="cancelled", reason="cancelled")

    async def cancel(self) -> None:
        await self.aclose()


class GeneratedAudio:
    def __init__(self, job: JobContract) -> None:
        self._job = job
        self._claimed = False
        self._evidence = job.snapshot()

    def evidence(self) -> GenerationEvidence:
        status = self._job.snapshot()
        if self._evidence.status == "running" and status.status in {"cancelled", "failed"}:
            return replace(self._evidence, status=status.status, reason=status.reason,
                           timings_ms=tuple(dict(self._evidence.timings_ms + status.timings_ms).items()))
        return replace(self._evidence, timings_ms=tuple(dict(self._evidence.timings_ms + status.timings_ms).items()))

    def frames(self) -> AsyncIterator[OutputFrame]:
        if self._claimed:
            raise RuntimeError("generated audio allows one consumer")
        self._claimed = True
        return _FrameStream(self, self._frames())

    async def _frames(self) -> AsyncIterator[OutputFrame]:
        terminal = False
        try:
            async for event in self._job.events():
                self._evidence = replace(self._evidence, last_event_seq=event.event_seq)
                if event.kind in {"text_final", "text_delta"}:
                    text = (event.text if event.kind == "text_final"
                            else self._evidence.text + event.text)
                    self._evidence = replace(self._evidence, text=text)
                elif event.kind == "timing":
                    self._evidence = replace(self._evidence, timings_ms=tuple(dict(self._evidence.timings_ms + event.timings_ms).items()))
                elif event.kind == "audio":
                    frame_seq = self._evidence.last_frame_seq + 1
                    self._evidence = replace(self._evidence, last_frame_seq=frame_seq)
                    yield OutputFrame(event.action_id, event.session, event.generation_id,
                                      frame_seq, event.pcm16, event.sample_rate)
                elif event.kind == "failed":
                    self._evidence = replace(self._evidence, status="failed", reason=event.reason)
                    raise OutputSourceError(event.reason)
                elif event.kind == "completed":
                    terminal = True
                    self._evidence = replace(self._evidence, status="completed")
            if not terminal:
                self._evidence = replace(self._evidence, status=self._job.snapshot().status,
                                         reason=self._job.snapshot().reason or "missing_terminal")
                raise OutputSourceError(self._evidence.reason)
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        await self._job.aclose()
        if self._evidence.status == "running":
            self._evidence = replace(self._evidence, status="cancelled", reason="consumer_closed")


class _FrameStream:
    """关闭尚未启动的流也关闭provider，不能依赖async generator的finally。"""
    def __init__(self, owner: GeneratedAudio, iterator):
        self._owner, self._iterator = owner, iterator
    def __aiter__(self):
        return self
    async def __anext__(self):
        return await self._iterator.__anext__()
    async def aclose(self):
        await self._iterator.aclose()
        await self._owner.aclose()
