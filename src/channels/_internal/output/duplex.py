"""浏览器全双工桥：收远端 PCM、推 TTS PCM，复用 Playwright voice backend。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any, cast

logger = logging.getLogger(__name__)

RemotePcmHandler = Callable[[str, bytes, int], Awaitable[None] | None]


class VoiceDuplex:
    """包装 BrowserVoiceTransport，增加 subscribe/publish_pcm。"""

    def __init__(self, transport: Any) -> None:
        self._transport = transport
        self._pcm_queue: asyncio.Queue[tuple[int, str, bytes, int, int]] = asyncio.Queue(maxsize=200)
        self._handler: RemotePcmHandler | None = None
        self._pump_task: asyncio.Task | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._capture_epoch = 0
        self._queued_bytes = 0
        self._gap_versions: dict[str, int] = {}
        self._delivered_versions: dict[str, int] = {}
        self._gap_handler: Callable[[str], None] | None = None
        self._handoff_lock = threading.Lock()
        self._handoff_bytes = self._handoff_frames = 0
        self._handoff_gaps: set[str] = set()
        self.output_volume: int = 50
        self.output_muted: bool = False

    def set_remote_pcm_handler(self, handler: RemotePcmHandler | None) -> None:
        self._handler = handler

    def set_input_gap_handler(self, handler: Callable[[str], None] | None) -> None:
        self._gap_handler = handler

    def enqueue_remote_pcm(self, uid: str, pcm: bytes, sample_rate: int) -> None:
        loop = self._owner_loop
        if loop is not None and not loop.is_closed():
            self._handoff(loop, self._capture_epoch, uid, pcm, sample_rate)

    def _mark_gap(self, uid, epoch=None):
        if epoch is not None and epoch != self._capture_epoch:
            return
        with self._handoff_lock:
            self._handoff_gaps.discard(uid)
        if uid in self._gap_versions or len(self._gap_versions) < 128:
            self._gap_versions[uid] = self._gap_versions.get(uid, 0) + 1

    def _handoff(self, loop, epoch, uid, pcm, sample_rate):
        if self._owner_loop is not loop or self._capture_epoch != epoch:
            return
        if type(uid) is not str or not uid or len(uid) > 256:
            return
        valid = (type(pcm) is bytes and bool(pcm) and len(pcm) <= 65536 and not len(pcm) % 2
                 and type(sample_rate) is int and 8000 <= sample_rate <= 96000)
        with self._handoff_lock:
            accepted = (valid and self._handoff_frames < 200
                        and self._handoff_bytes + len(pcm) <= 4 * 1024 * 1024)
            if accepted:
                self._handoff_bytes += len(pcm)
                self._handoff_frames += 1
            elif uid not in self._handoff_gaps and len(self._handoff_gaps) < 128:
                self._handoff_gaps.add(uid)
                loop.call_soon_threadsafe(self._mark_gap, uid, epoch)
        if not accepted:
            return
        def deliver():
            with self._handoff_lock:
                self._handoff_bytes -= len(pcm)
                self._handoff_frames -= 1
            self._enqueue_on_owner(loop, epoch, uid, pcm, sample_rate)
        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            with self._handoff_lock:
                self._handoff_bytes -= len(pcm)
                self._handoff_frames -= 1

    def _enqueue_on_owner(
        self, loop: asyncio.AbstractEventLoop, epoch: int, uid: str, pcm: bytes, sample_rate: int,
    ) -> None:
        if self._owner_loop is not loop or self._capture_epoch != epoch:
            return
        if uid not in self._gap_versions and len(self._gap_versions) >= 128:
            return
        self._gap_versions.setdefault(uid, 0)
        if self._queued_bytes + len(pcm) > 4 * 1024 * 1024 or self._pcm_queue.full():
            self._mark_gap(uid)
            return
        self._pcm_queue.put_nowait((epoch, uid, pcm, sample_rate, self._gap_versions[uid]))
        self._queued_bytes += len(pcm)

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
            self._handoff(loop, epoch, uid, pcm, sample_rate)

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
        self._queued_bytes = 0
        self._gap_versions.clear()
        self._delivered_versions.clear()
        await self._transport.close()

    async def _pump(self) -> None:
        while True:
            epoch, uid, pcm, rate, version = await self._pcm_queue.get()
            self._queued_bytes -= len(pcm)
            if epoch != self._capture_epoch or version != self._gap_versions.get(uid, 0):
                continue
            if version != self._delivered_versions.get(uid, 0):
                self._delivered_versions[uid] = version
                if self._gap_handler is not None:
                    self._gap_handler(uid)
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
