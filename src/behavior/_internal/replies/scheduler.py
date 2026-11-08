"""最新输入信箱和回复冷却；不拥有模型任务或房间状态。"""
import asyncio
import time
from collections.abc import Awaitable, Callable

from behavior.models import ScheduledReply


class LatestReplies:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self._clock, self._sleep = clock, sleep
        self._pending: ScheduledReply | None = None
        self._wake = asyncio.Event()
        self._reset_wake = asyncio.Event()
        self._generation = 0
        self._sequence = 0
        self._deadline = 0.0
        self._active: ScheduledReply | None = None

    def reset(self) -> None:
        self._reset_wake.set()
        self._reset_wake = asyncio.Event()
        self._generation += 1
        self._pending = self._active = None
        self._deadline = 0.0
        self._wake.set()

    def offer(self, user_key: str, pcm: bytes, sample_rate: int, *, input_ref: str = "") -> None:
        self._sequence += 1
        self._pending = ScheduledReply(user_key, pcm, sample_rate, self._clock(),
                                       self._generation, self._sequence, input_ref)
        self._wake.set()

    async def next(self, cooldown: float) -> ScheduledReply:
        """单消费者等待；过期输入丢弃后继续等候，取消不吞掉后续输入。"""
        while True:
            await self._wake.wait()
            self._wake.clear()
            item, self._pending = self._pending, None
            if item is None:
                continue
            prepared = await self.prepare(item, cooldown)
            if prepared is not None:
                return prepared

    async def prepare(self, item: ScheduledReply, cooldown: float) -> ScheduledReply | None:
        """兼容已有直接驱动；调度与替换规则仍只有本对象拥有。"""
        generation = self._generation
        if item.generation != generation:
            return None
        remaining = self._deadline - self._clock()
        if remaining > 0:
            sleeper = asyncio.ensure_future(self._sleep(remaining))
            reset = asyncio.create_task(self._reset_wake.wait())
            try:
                await asyncio.wait((sleeper, reset), return_when=asyncio.FIRST_COMPLETED)
                if sleeper.done():
                    await sleeper
            finally:
                sleeper.cancel()
                reset.cancel()
                await asyncio.gather(sleeper, reset, return_exceptions=True)
            if generation != self._generation or item.generation != self._generation:
                return None
            if self._pending is not None:
                item, self._pending = self._pending, None
                self._wake.clear()
        if generation != self._generation or item.generation != self._generation:
            return None
        if self._clock() - item.offered_at > max(0, cooldown) + 3:
            return None
        self._active = item
        return item

    def finish(self, item: ScheduledReply, *, cooldown: float, interrupted: bool = False) -> None:
        if self._active != item or item.generation != self._generation:
            return
        self._active = None
        self._deadline = 0.0 if interrupted else self._clock() + max(0, cooldown)
