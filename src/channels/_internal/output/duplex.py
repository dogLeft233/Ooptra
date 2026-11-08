"""浏览器全双工桥：收远端 PCM、推 TTS PCM，复用 Playwright voice backend。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

logger = logging.getLogger(__name__)

RemotePcmHandler = Callable[[str, bytes, int], Awaitable[None] | None]


class VoiceDuplex:
    """包装 BrowserVoiceTransport，增加 subscribe/publish_pcm。"""

    def __init__(self, transport: Any) -> None:
        self._transport = transport
        self._pcm_queue: asyncio.Queue[tuple[int, str, bytes, int]] = asyncio.Queue(maxsize=200)
        self._handler: RemotePcmHandler | None = None
        self._pump_task: asyncio.Task | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._capture_epoch = 0
        self.output_volume: int = 50
        self.output_muted: bool = False

    @property
    def transport(self) -> Any:
        return self._transport

    def set_remote_pcm_handler(self, handler: RemotePcmHandler | None) -> None:
        self._handler = handler

    def enqueue_remote_pcm(self, uid: str, pcm: bytes, sample_rate: int) -> None:
        loop = self._owner_loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._enqueue_on_owner, loop, self._capture_epoch, uid, pcm, sample_rate)
        except RuntimeError:
            logger.debug("remote pcm owner loop closed, drop chunk")

    def _enqueue_on_owner(
        self, loop: asyncio.AbstractEventLoop, epoch: int, uid: str, pcm: bytes, sample_rate: int,
    ) -> None:
        if self._owner_loop is not loop or self._capture_epoch != epoch:
            return
        try:
            self._pcm_queue.put_nowait((epoch, uid, pcm, sample_rate))
        except asyncio.QueueFull:
            logger.debug("remote pcm queue full, drop chunk")

    async def start(self) -> None:
        if self._pump_task is not None and not self._pump_task.done():
            return
        await self._transport.start()
        self._owner_loop = asyncio.get_running_loop()
        self._capture_epoch += 1
        epoch = self._capture_epoch

        def captured(uid: str, pcm: bytes, sample_rate: int) -> None:
            loop = self._owner_loop
            if loop is None or loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(self._enqueue_on_owner, loop, epoch, uid, pcm, sample_rate)
            except RuntimeError:
                logger.debug("remote pcm owner loop closed, drop chunk")

        self._transport.set_remote_pcm_callback(captured)
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump(), name="voice-duplex-pump")

    async def close(self) -> None:
        self._owner_loop = None
        self._capture_epoch += 1
        self._transport.set_remote_pcm_callback(None)
        task = self._pump_task
        self._pump_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        while not self._pcm_queue.empty():
            self._pcm_queue.get_nowait()
        await self._transport.close()

    async def _pump(self) -> None:
        while True:
            epoch, uid, pcm, rate = await self._pcm_queue.get()
            if epoch != self._capture_epoch:
                continue
            handler = self._handler
            if handler is None:
                continue
            try:
                result = handler(uid, pcm, rate)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("remote pcm handler failed")

    async def enable_listen(self, enabled: bool = True) -> dict[str, Any]:
        return await self._transport.enable_listen(enabled)

    async def push_tts_pcm(
        self,
        pcm16: bytes,
        sample_rate: int,
        *,
        finish: bool = False,
    ) -> dict[str, Any]:
        if not pcm16 and not finish:
            return {"ok": True, "empty": True}
        return await self._transport.push_tts_pcm(
            pcm16, sample_rate, finish=finish, volume=0 if self.output_muted else self.output_volume,
        )

    async def set_output_volume(self, volume: int) -> dict[str, Any]:
        self.output_volume = volume
        result = await self._transport.set_tts_volume(0 if self.output_muted else volume)
        # Oopz's member indicator uses a separate state broadcast from the audio gain.
        broadcast = getattr(self._transport, "set_voice_state", None)
        if result.get("ok") and callable(broadcast):
            broadcast_state = cast(Callable[..., Awaitable[bool]], broadcast)
            try:
                if not await broadcast_state(mic_muted=self.output_muted, speaker_muted=False):
                    logger.warning("语音音频已应用，但房间麦克风状态广播失败")
            except Exception:
                logger.warning("语音音频已应用，但房间麦克风状态广播失败", exc_info=True)
        return result

    async def stop_tts(self) -> dict[str, Any]:
        return await self._transport.stop_tts()

    async def wait_tts_complete(self, timeout: float = 30.0) -> dict[str, Any]:
        return await self._transport.wait_tts_complete(timeout)
