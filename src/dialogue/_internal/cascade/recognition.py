"""单次受许可识别的有界双向音频流；临时转录仅为可替换投影。"""
from __future__ import annotations

import asyncio
import time

import aiohttp

from dialogue.models import AudioInput, Recognition, TranscriptRevision


class StreamingRecognition:
    def __init__(self, backend, input: AudioInput, permit):
        if (input.session != permit.session or input.input_ref != permit.input_ref
                or input.attempt_id != permit.attempt_id or permit.expires_at <= time.monotonic()
                or input.pcm16):
            raise ValueError("流式识别需要有效原许可和空音频头")
        self._backend, self._input = backend, input
        self._deadline = permit.expires_at
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(64)
        self._bytes = 0
        self._ended = False
        self._input_ended_at = 0.0
        self._partial: dict[int, TranscriptRevision] = {}
        self._task = asyncio.create_task(self._run(), name="dialogue-streaming-recognition")

    def feed(self, pcm16: bytes) -> None:
        if self._ended or self._task.done():
            raise ValueError("识别输入已关闭")
        if (type(pcm16) is not bytes or not pcm16 or len(pcm16) % 2
                or len(pcm16) > 65536):
            raise ValueError("识别帧须为不超过64KiB的完整PCM16")
        self._bytes += len(pcm16)
        if self._bytes > self._input.sample_rate * 2 * 60:
            self._task.cancel()
            raise ValueError("识别输入超过60秒")
        try:
            self._queue.put_nowait(pcm16)
        except asyncio.QueueFull:
            self._task.cancel()
            raise ValueError("识别队列溢出，输入不连续") from None

    def finish_input(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._input_ended_at = time.monotonic()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            self._task.cancel()
            raise ValueError("识别队列溢出，无法正常结束") from None

    def partial(self) -> tuple[TranscriptRevision, ...]:
        return tuple(self._partial[key] for key in sorted(self._partial))

    async def result(self) -> Recognition:
        return await asyncio.shield(self._task)

    async def aclose(self) -> None:
        self._ended = True
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        if self._backend._asr_streams.get(self._input.attempt_id) is self:
            self._backend._asr_streams.pop(self._input.attempt_id)

    async def _run(self) -> Recognition:
        input = self._input
        started = time.monotonic()
        try:
            text = await asyncio.wait_for(self._exchange(), max(.001, self._deadline-started))
            return Recognition(input.input_ref, input.session, input.attempt_id, input.user_key,
                               "recognized" if text else "no_speech", text,
                               elapsed_ms=(time.monotonic()-started)*1000,
                               finalize_ms=max(0, time.monotonic()-(self._input_ended_at or time.monotonic()))*1000)
        except asyncio.CancelledError:
            raise
        except Exception:
            return Recognition(input.input_ref, input.session, input.attempt_id, input.user_key,
                               "failed", reason="streaming_recognition_unavailable",
                               elapsed_ms=(time.monotonic()-started)*1000,
                               finalize_ms=max(0, time.monotonic()-(self._input_ended_at or time.monotonic()))*1000)

    async def _exchange(self) -> str:
        # Local import avoids a protocol/parser cycle; only this adapter knows DashScope.
        from uuid import uuid4

        from dialogue._internal.cascade.aliyun_deepseek import check_event, task_event
        from dialogue._internal.cascade.mimo_cascade import resample_pcm16, sanitize_asr

        backend, input = self._backend, self._input
        http = await backend._http()
        task_id = uuid4().hex
        async with http.ws_connect(backend.settings.dashscope_ws_url,
                                   headers=backend._speech_headers(), proxy=backend._proxy_url(),
                                   max_msg_size=2*1024*1024) as ws:
            await ws.send_json(task_event("run-task", task_id, {
                "task_group": "audio", "task": "asr", "function": "recognition",
                "model": backend.settings.aliyun_asr_model,
                "parameters": {"format": "pcm", "sample_rate": 16000,
                               "intermediate_result_enabled": True,
                               "disfluency_removal_enabled": False}, "input": {}}))
            await backend._started(ws, task_id)

            async def send():
                while (pcm := await self._queue.get()) is not None:
                    pcm = resample_pcm16(pcm, input.sample_rate, 16000)
                    for offset in range(0, len(pcm), 3200):
                        await ws.send_bytes(pcm[offset:offset+3200])
                await ws.send_json(task_event("finish-task", task_id, {"input": {}}))

            sender = asyncio.create_task(send(), name="dialogue-asr-upload")
            try:
                while True:
                    message = await ws.receive(timeout=30)
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise ValueError("识别流缺少正常终态")
                    data = message.json()
                    event = check_event(data, task_id)
                    if event == "result-generated":
                        sentence = (data.get("payload", {}).get("output") or {}).get("sentence") or {}
                        if sentence.get("heartbeat"):
                            continue
                        key, text = sentence.get("sentence_id"), sentence.get("text")
                        final = sentence.get("sentence_end")
                        if type(key) is not int or key < 0 or type(text) is not str or type(final) is not bool:
                            raise ValueError("识别结果字段非法")
                        if key not in self._partial and len(self._partial) >= 256:
                            raise ValueError("识别句子超过预算")
                        if len(text) > 8192:
                            raise ValueError("识别文本超过预算")
                        previous = self._partial.get(key)
                        if previous is not None and previous.final and (not final or previous.text != text):
                            raise ValueError("最终转录发生修正")
                        self._partial[key] = TranscriptRevision(key, text, final)
                    if event == "task-finished":
                        if not self._ended:
                            raise ValueError("输入结束前识别流提前关闭")
                        await sender
                        if any(not row.final for row in self._partial.values()):
                            raise ValueError("识别结束仍有未确认临时文本")
                        return sanitize_asr("".join(row.text for row in self.partial()))
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
