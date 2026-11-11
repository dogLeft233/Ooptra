"""迁移期单次播放证据；不包含生成选择或历史提交规则。"""

from __future__ import annotations

import asyncio

from channels.api import Channel, Duplex
from channels.models import AudioPushResult, PlaybackResult, SessionRef


class RoomPlayback:
    def __init__(self, channel: Channel, session: SessionRef | None, output: Duplex) -> None:
        self._channel, self._session, self._output = channel, session, output
        self._lock = asyncio.Lock()
        self._submitted = 0
        self._failed = False
        self._suppressed = False
        self._interrupted = False

    def interrupt(self) -> None:
        self._interrupted = True

    async def push(self, pcm: bytes, rate: int) -> AudioPushResult:
        async with self._lock:
            if self._interrupted:
                return AudioPushResult(False, "interrupted")
            self._suppressed |= bool(self._output.output_muted)
            result = await self._push(pcm, rate)
            self._failed |= not result.ok
            if result.ok:
                self._submitted += len(pcm)
            return result

    async def _push(self, pcm: bytes, rate: int, *, finish: bool = False) -> AudioPushResult:
        if self._session is None:
            return AudioPushResult(False, "stale_session")
        work = asyncio.create_task(self._channel.push_audio(self._session, pcm, rate, finish=finish))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            self.interrupt()
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not work.cancelled():
                work.exception()
            raise

    async def finish(self, rate: int) -> PlaybackResult:
        async with self._lock:
            if self._interrupted:
                return PlaybackResult("interrupted", self._submitted)
            if self._failed:
                return PlaybackResult("failed", self._submitted)
            if not self._submitted:
                return PlaybackResult("silent")
            if self._channel.snapshot().session != self._session:
                return PlaybackResult("failed", self._submitted)
            result = await self._push(b"", rate, finish=True)
        drained = False
        if result.ok:
            try:
                drained = bool((await asyncio.wait_for(self._output.wait_tts_complete(30), 30)).get("ok"))
            except (TimeoutError, OSError):
                drained = False
        if self._interrupted:
            return PlaybackResult("interrupted", self._submitted)
        if not result.ok or not drained or self._channel.snapshot().session != self._session:
            return PlaybackResult("failed", self._submitted)
        if self._suppressed or self._output.output_muted:
            return PlaybackResult("silent", self._submitted)
        return PlaybackResult("completed", self._submitted)

    async def stop(self) -> None:
        self.interrupt()
        async with self._lock:
            if self._channel.snapshot().session == self._session:
                await self._output.stop_tts()
