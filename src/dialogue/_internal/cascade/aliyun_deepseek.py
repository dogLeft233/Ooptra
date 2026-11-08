"""Aliyun Message ASR + DeepSeek SSE + bidirectional Qwen Audio TTS.

Keep the existing per-member VAD, reply policy, controls and memory behavior.
ASR uploads each completed utterance over WebSocket. Ordinary replies stream
sentence by sentence into TTS; PCM reaches the room before the turn finishes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import uuid4

import aiohttp

from behavior.models import VoiceControlDecision
from dialogue._internal.cascade.mimo_cascade import (
    MimoCascadeBackend,
    clamp_reply,
    looks_like_reasoning_leak,
    resample_pcm16,
    sanitize_asr,
    sanitize_for_tts,
)
from dialogue.models import (
    AudioSink,
    DialogueCapabilities,
    VoiceReply,
    VoiceStreamError,
    VoiceStreamInterrupted,
)


async def sse_events(content) -> AsyncIterator[dict]:
    """Read SSE records, including split TCP chunks and multi-line data fields."""
    lines: list[str] = []
    async for raw in content:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
        elif not line and lines:
            payload = "\n".join(lines)
            lines.clear()
            if payload == "[DONE]":
                return
            event = json.loads(payload)
            if not isinstance(event, dict) or event.get("error"):
                raise RuntimeError("DeepSeek stream returned an error")
            yield event
    raise RuntimeError("DeepSeek stream ended without [DONE]")


def task_event(action: str, task_id: str, payload: dict) -> dict:
    return {"header": {"action": action, "task_id": task_id, "streaming": "duplex"},
            "payload": payload}


def check_event(data: dict, task_id: str) -> str:
    header = data.get("header") or {}
    if header.get("task_id") != task_id:
        raise RuntimeError("DashScope returned a mismatched task ID")
    event = header.get("event", "")
    if event == "task-failed":
        # Provider messages can contain request details; never expose them or keys.
        raise RuntimeError(f"DashScope task failed ({header.get('error_code', 'unknown')})")
    return event


class AliyunDeepSeekBackend(MimoCascadeBackend):
    name = "aliyun_deepseek"

    def capabilities(self) -> DialogueCapabilities:
        return DialogueCapabilities(streaming_utterance=True)

    def __init__(self, settings, memory, *, reply_policy) -> None:
        super().__init__(settings, memory, reply_policy=reply_policy)
        self._audio_sink: AudioSink | None = None
        self._tts_task: asyncio.Task[bytes] | None = None
        self._texts: asyncio.Queue[str | None] | None = None

    def _headers(self) -> dict[str, str]:
        if not self.settings.deepseek_api_key:
            raise RuntimeError("请在 .env.local 配置 DEEPSEEK_API_KEY")
        return {"Authorization": f"Bearer {self.settings.deepseek_api_key}",
                "Content-Type": "application/json"}

    def _speech_headers(self) -> dict[str, str]:
        if not self.settings.dashscope_api_key:
            raise RuntimeError("请在 .env.local 配置 DASHSCOPE_API_KEY")
        return {"Authorization": f"Bearer {self.settings.dashscope_api_key}"}

    async def _started(self, ws, task_id: str) -> None:
        message = await ws.receive(timeout=15)
        if message.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError("DashScope closed before starting the task")
        if check_event(json.loads(message.data), task_id) != "task-started":
            raise RuntimeError("DashScope did not acknowledge task start")

    async def asr(self, pcm16: bytes, sample_rate: int) -> str:
        if not pcm16:
            return ""
        pcm16 = resample_pcm16(pcm16, sample_rate, 16000)
        session = await self._http()
        task_id = uuid4().hex
        async with session.ws_connect(
            self.settings.dashscope_ws_url, headers=self._speech_headers(),
            proxy=self._proxy_url(), max_msg_size=2 * 1024 * 1024,
        ) as ws:
            await ws.send_json(task_event("run-task", task_id, {
                "task_group": "audio", "task": "asr", "function": "recognition",
                "model": self.settings.aliyun_asr_model,
                "parameters": {"format": "pcm", "sample_rate": 16000,
                               "disfluency_removal_enabled": False}, "input": {},
            }))
            await self._started(ws, task_id)

            async def send_audio():
                for offset in range(0, len(pcm16), 3200):
                    await ws.send_bytes(pcm16[offset:offset + 3200])
                await ws.send_json(task_event("finish-task", task_id, {"input": {}}))

            sender = asyncio.create_task(send_audio())
            sentences: dict[int, str] = {}
            try:
                while True:
                    message = await ws.receive(timeout=30)
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise RuntimeError("DashScope ASR closed before task-finished")
                    data = json.loads(message.data)
                    event = check_event(data, task_id)
                    if event == "result-generated":
                        sentence = (data.get("payload", {}).get("output") or {}).get("sentence") or {}
                        if sentence.get("sentence_end") and not sentence.get("heartbeat"):
                            sentences[int(sentence["sentence_id"])] = sentence.get("text") or ""
                    if event == "task-finished":
                        await sender
                        return sanitize_asr("".join(sentences[i] for i in sorted(sentences)))
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    async def chat_with_intent(
        self, user_text: str, *, user_key: str = "",
    ) -> tuple[VoiceControlDecision, str]:
        # Validate controls before opening an audible stream. JSON is never spoken.
        decision = await self.detect_control_intent(user_text)
        if not self.intent_response_valid:
            raise ValueError("Invalid voice control decision")
        if decision.leave or decision.volume is not None:
            return decision, ""
        try:
            return decision, await self.chat(user_text, user_key=user_key)
        except VoiceStreamInterrupted:
            raise
        except Exception as exc:
            raise VoiceStreamError("DeepSeek/TTS 流式回复失败") from exc

    async def _chat_completion(self, body: dict, *, stage: str = "LLM") -> dict:
        # Tool and control responses must be completely validated before speech.
        streaming = self._audio_sink is not None and not body.get("response_format") and not body.get("tools")
        request = {**body, "model": self.settings.deepseek_model, "stream": streaming}
        session = await self._http()
        async with session.post(
            self.settings.deepseek_base_url.rstrip("/") + "/chat/completions",
            headers=self._headers(), json=request, proxy=self._proxy_url(),
        ) as response:
            if response.status >= 400:
                raise RuntimeError(f"DeepSeek {stage} failed HTTP {response.status}")
            if not streaming:
                return await response.json()
            raw, spoken = "", ""
            finished = False
            async for event in sse_events(response.content):
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                if delta.get("tool_calls"):
                    raise RuntimeError("Unexpected tool call in speech stream")
                raw += delta.get("content") or ""
                if len(raw) > 32768:
                    raise RuntimeError("DeepSeek reply exceeded the stream limit")
                clean = clamp_reply(sanitize_for_tts(raw))
                end = max((clean.rfind(mark) for mark in "。！？!?；;\n"), default=-1) + 1
                if end > len(spoken) and not looks_like_reasoning_leak(clean):
                    complete = clean[:end]
                    if not complete.startswith(spoken):
                        raise RuntimeError("Streaming reply changed previously spoken text")
                    await self._queue_text(complete[len(spoken):])
                    spoken = complete
                reason = choice.get("finish_reason")
                if reason:
                    if reason not in {"stop", "length"}:
                        raise RuntimeError("DeepSeek did not finish a normal reply")
                    finished = True
            if not finished:
                raise RuntimeError("DeepSeek stream missing completion event")
            clean = clamp_reply(sanitize_for_tts(raw))
            if not looks_like_reasoning_leak(clean):
                if not clean.startswith(spoken):
                    raise RuntimeError("Final reply differs from spoken text")
                await self._queue_text(clean[len(spoken):])
            if self._texts is not None:
                await self._texts.put(None)
            return {"choices": [{"message": {"role": "assistant", "content": raw}}]}

    async def _queue_text(self, text: str) -> None:
        if not text:
            return
        if self._texts is None:
            self._texts = asyncio.Queue()
            self._tts_task = asyncio.create_task(self._synthesize(self._queued_texts()))
        if self._tts_task is not None and self._tts_task.done():
            await self._tts_task
        await self._texts.put(text)

    async def _queued_texts(self) -> AsyncIterator[str]:
        assert self._texts is not None
        while (text := await self._texts.get()) is not None:
            yield text

    async def _synthesize(self, texts: AsyncIterator[str]) -> bytes:
        session = await self._http()
        task_id = uuid4().hex
        async with session.ws_connect(
            self.settings.dashscope_ws_url, headers=self._speech_headers(),
            proxy=self._proxy_url(), max_msg_size=2 * 1024 * 1024,
        ) as ws:
            await ws.send_json(task_event("run-task", task_id, {
                "task_group": "audio", "task": "tts", "function": "SpeechSynthesizer",
                "model": self.settings.aliyun_tts_model,
                "parameters": {"text_type": "PlainText", "voice": self.settings.aliyun_tts_voice,
                               "format": "pcm", "sample_rate": self.settings.sample_rate_out},
                "input": {},
            }))
            await self._started(ws, task_id)

            async def send_text():
                async for text in texts:
                    await ws.send_json(task_event("continue-task", task_id, {"input": {"text": text}}))
                await ws.send_json(task_event("finish-task", task_id, {"input": {}}))

            sender = asyncio.create_task(send_text())
            pcm = bytearray()
            pending = b""
            try:
                while True:
                    message = await ws.receive(timeout=30)
                    if message.type == aiohttp.WSMsgType.BINARY:
                        pending += message.data
                        size = len(pending) // 2 * 2
                        chunk, pending = pending[:size], pending[size:]
                        pcm.extend(chunk)
                        if len(pcm) > self.settings.sample_rate_out * 2 * 60:
                            raise RuntimeError("TTS audio exceeded 60 seconds")
                        if chunk and self._audio_sink is not None:
                            await self._audio_sink(chunk, self.settings.sample_rate_out)
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        event = check_event(json.loads(message.data), task_id)
                        if event == "task-finished":
                            await sender
                            if pending or not pcm:
                                raise RuntimeError("TTS returned incomplete or empty PCM")
                            return bytes(pcm)
                    else:
                        raise RuntimeError("DashScope TTS closed before task-finished")
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    async def tts(self, text: str) -> tuple[bytes, int]:
        if self._tts_task is not None:
            return await self._tts_task, self.settings.sample_rate_out

        async def single_text():
            clean = sanitize_for_tts(text)
            if clean:
                yield clean

        if not sanitize_for_tts(text):
            return b"", self.settings.sample_rate_out
        return await self._synthesize(single_text()), self.settings.sample_rate_out

    async def handle_utterance_stream(
        self, pcm16: bytes, *, sample_rate: int, user_key: str, channel_key: str,
        on_audio: AudioSink,
    ) -> VoiceReply:
        async with self._generation_lock:
            self._audio_sink = on_audio
            try:
                return await self._generate_utterance(
                    pcm16, sample_rate=sample_rate, user_key=user_key, channel_key=channel_key,
                )
            finally:
                await self._clear_stream()

    async def _clear_stream(self) -> None:
        task, self._tts_task = self._tts_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._texts = None
        self._audio_sink = None

    async def aclose(self) -> None:
        await self._clear_stream()
        await super().aclose()
