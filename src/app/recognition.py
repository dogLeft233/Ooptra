"""应用拥有识别流程租约；协议、音频授权和准入分别归生成、渠道、行为。"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from uuid import uuid4

from behavior.models import AttemptFinished, AttemptPermit, AttemptRejected, AudioReady
from channels.models import IncomingAudio, UtteranceResult
from dialogue.api import RecognitionStream
from dialogue.models import AudioInput

logger = logging.getLogger(__name__)


@dataclass
class PreparedRecognition:
    trigger: AudioReady
    permit: AttemptPermit
    stream: RecognitionStream
    rate: int
    finished: bool = False
    expiry: asyncio.Task | None = None


class RecognitionInputs:
    """最多8份识别租约；被信箱替换的输入在冷却+4秒后关闭，不进入记忆。"""
    def __init__(self, host):
        self.host = host
        self._active: dict[str, PreparedRecognition] = {}
        self._ready: dict[str, PreparedRecognition] = {}
        self._prefix: dict[str, tuple[int, bytearray]] = {}
        self._closing: set[asyncio.Task] = set()

    async def _close(self, item: PreparedRecognition, reason: str):
        if item.expiry is not None and item.expiry is not asyncio.current_task():
            item.expiry.cancel()
            await asyncio.gather(item.expiry, return_exceptions=True)
        try:
            await item.stream.aclose()
        finally:
            if not item.finished:
                item.finished = True
                self.host.behavior.finish_attempt(AttemptFinished(item.permit, reason, time.monotonic()))

    def invalidate(self):
        """所属loop的同步断点通知；立即撤回租约，异步关闭由本拥有者观察。"""
        items = list(self._active.values()) + list(self._ready.values())
        self._active.clear()
        self._ready.clear()
        self._prefix.clear()
        for item in items:
            task = asyncio.create_task(self._close(item, "input_gap"), name="app-asr-gap-cleanup")
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    async def reset(self):
        self.invalidate()
        if self._closing:
            await asyncio.gather(*tuple(self._closing), return_exceptions=True)

    async def feed(self, audio: IncomingAudio, cut: UtteranceResult) -> str:
        uid = audio.user_key
        item = self._active.get(uid)
        session = self.host.channels.snapshot().session
        if cut.status == "gap":
            await self.reset()
            return ""
        if item and (item.rate != audio.sample_rate or item.trigger.session != session):
            self._active.pop(uid)
            await self._close(item, "input_changed")
            item = None
            self._prefix.pop(uid, None)
        if cut.status in {"noise", "too_short"}:
            self._prefix.pop(uid, None)
            if item:
                self._active.pop(uid)
                await self._close(item, cut.status)
            return ""
        if cut.stream_pcm16:
            if item is None:
                if len(self._active)+len(self._ready)+len(self._closing) >= 8 or session is None:
                    return ""
                rate, prefix = self._prefix.setdefault(uid, (audio.sample_rate, bytearray()))
                if rate != audio.sample_rate:
                    prefix = bytearray()
                    self._prefix[uid] = audio.sample_rate, prefix
                prefix.extend(cut.stream_pcm16)
                if len(prefix) < audio.sample_rate*2*self.host._input_policy().min_utterance_ms/1000:
                    return ""
                revision = self.host.context_service.snapshot(session.room_target).revision
                trigger = AudioReady(uuid4().hex, session, "oopz:"+uid, revision)
                permit = self.host.behavior.admit(trigger, self.host.settings.cascade_turn_limit)
                self._prefix.pop(uid, None)
                if isinstance(permit, AttemptRejected):
                    return ""
                try:
                    stream = self.host.backend.open_recognition(AudioInput(
                        trigger.trigger_id, session, b"", audio.sample_rate, trigger.user_key, permit.attempt_id), permit)
                except Exception:
                    self.host.behavior.finish_attempt(AttemptFinished(permit, "asr_start_failed", time.monotonic()))
                    return ""
                item = PreparedRecognition(trigger, permit, stream, audio.sample_rate)
                self._active[uid] = item
                logger.info("event=voice.asr.started session_id=%s input_ref=%s streaming=true",
                            session.session_id, trigger.trigger_id)
                item.expiry = asyncio.create_task(self._expire(uid, item, 60), name="app-asr-capture-expiry")
                chunks = bytes(prefix)
            else:
                chunks = cut.stream_pcm16
            try:
                for offset in range(0, len(chunks), 65536):
                    item.stream.feed(chunks[offset:offset+65536])
            except ValueError:
                self._active.pop(uid, None)
                await self._close(item, "asr_input_failed")
                return ""
        if cut.status == "ready" and item:
            self._active.pop(uid, None)
            try:
                item.stream.finish_input()
            except ValueError:
                await self._close(item, "asr_input_failed")
                return ""
            if item.expiry:
                item.expiry.cancel()
                await asyncio.gather(item.expiry, return_exceptions=True)
            key = item.trigger.trigger_id
            self._ready[key] = item
            item.expiry = asyncio.create_task(self._expire(key, item, self.host._reply_cooldown()+4),
                                               name="app-asr-selection-expiry")
            return key
        return ""

    async def _expire(self, key, item, seconds):
        await asyncio.sleep(seconds)
        try:
            await self._close(item, "unselected_or_expired")
        finally:
            if self._active.get(key) is item:
                self._active.pop(key)
            if self._ready.get(key) is item:
                self._ready.pop(key)

    def take(self, key: str) -> PreparedRecognition | None:
        # Transfer ownership without an await/cancellation gap after removing the lease.
        item = self._ready.pop(key, None)
        if item and item.expiry:
            item.expiry.cancel()
            self._closing.add(item.expiry)
            item.expiry.add_done_callback(self._closing.discard)
            item.expiry = None
        return item

    async def release(self, item: PreparedRecognition):
        await self._close(item, "unexecuted")
