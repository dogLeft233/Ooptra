"""Persisted speech volume and temporary output muting share one controller."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from channels.api import OutputSettings


class VoiceOutputControls:
    def __init__(self, settings: OutputSettings, duplex: Callable[[], Any], joined: Callable[[], bool]):
        self.settings = settings
        self._duplex = duplex
        self._joined = joined
        self._saver: Callable[[int], Awaitable[None]] | None = None
        self._lock = asyncio.Lock()
        self.muted = False
        self._generation = 0

    @asynccontextmanager
    async def configuration_update(self):
        """Serialize config reloads with commands that save and apply volume."""
        async with self._lock:
            yield

    def set_saver(self, saver: Callable[[int], Awaitable[None]]) -> None:
        self._saver = saver

    def attach(self, duplex: Any) -> None:
        duplex.output_volume = self.settings.output_volume
        duplex.output_muted = self.muted

    def reset_mute(self) -> None:
        self._generation += 1
        self.muted = False
        if (duplex := self._duplex()) is not None:
            self.attach(duplex)

    async def sync_volume(self, *, unmute: bool = False) -> None:
        generation = self._generation
        previous_muted = self.muted
        if unmute:
            self.muted = False
        duplex = self._duplex()
        if duplex is None:
            return
        self.attach(duplex)
        if self._joined():
            try:
                result = await duplex.set_output_volume(self.settings.output_volume)
                if generation != self._generation:
                    raise RuntimeError("语音会话已切换，旧操作已失效")
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "语音输出应用失败")
            except Exception:
                if generation == self._generation:
                    self.muted = previous_muted if self._joined() else False
                if (current := self._duplex()) is not None:
                    self.attach(current)
                raise

    def snapshot(self) -> dict[str, Any]:
        return {"volume": self.settings.output_volume, "muted": self.muted}

    async def set(self, *, volume: int | None = None, muted: bool | None = None) -> dict[str, Any]:
        async with self._lock:
            return await self._change(volume, muted)

    async def adjust(self, target: Callable[[int], tuple[int | None, bool | None]]) -> dict[str, Any]:
        """在音量锁内将相对策略解析为绝对目标，避免并发增量丢失。"""
        async with self._lock:
            volume, muted = target(self.settings.output_volume)
            return await self._change(volume, muted)

    async def _change(self, volume: int | None, muted: bool | None) -> dict[str, Any]:
        if volume is None and muted is None:
            return {"ok": False, "error": "请提供音量或麦克风状态"}
        if volume is not None and (type(volume) is not int or not 10 <= volume <= 100):
            return {"ok": False, "error": "音量必须为 10-100 的整数"}
        if muted is not None and (type(muted) is not bool or not self._joined()):
            return {"ok": False, "error": "进房后才能控制麦克风"}
        saved = False
        previous_muted = self.muted
        generation = self._generation
        try:
            if volume is not None:
                if self._saver is None:
                    raise RuntimeError("音量保存服务尚未就绪")
                # Threaded config writes cannot be stopped by cancelling the voice
                # worker. Let the write settle before releasing the shared lock.
                save_task = asyncio.ensure_future(self._saver(volume))
                cancelled = False
                while True:
                    try:
                        await asyncio.shield(save_task)
                        break
                    except asyncio.CancelledError:
                        if save_task.cancelled():
                            raise
                        cancelled = True
                    except Exception:
                        if cancelled:
                            raise asyncio.CancelledError from None
                        raise
                self.settings.output_volume = volume
                saved = True
                if cancelled:
                    if (duplex := self._duplex()) is not None:
                        self.attach(duplex)
                    raise asyncio.CancelledError
                if generation != self._generation:
                    raise RuntimeError("音量已保存，但语音会话已切换，旧操作已失效")
                self.muted = False
            if muted is not None:
                if not self._joined():
                    raise RuntimeError("语音会话已结束，麦克风状态未修改")
                self.muted = muted
            await self.sync_volume()
        except Exception as exc:
            if generation == self._generation:
                self.muted = previous_muted if self._joined() else False
            if (duplex := self._duplex()) is not None:
                self.attach(duplex)
            return {"ok": False, **self.snapshot(), "saved": saved, "error": str(exc)}
        message = ("麦克风已关闭，仍在接听" if self.muted else "麦克风已打开") if muted is not None else f"语音音量已设为 {volume}% 并保存，麦克风已打开"
        return {"ok": True, **self.snapshot(), "saved": saved, "message": message}
