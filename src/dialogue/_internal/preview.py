"""试听能力独占临时供应商连接；没有房间、行为许可和记忆授权。"""
from __future__ import annotations

import asyncio
import base64
import copy
import io
import wave
from typing import Literal
from uuid import uuid4

from dialogue.api import VoiceLibrary
from dialogue.models import PreviewArtifact, PreviewFailure, PreviewRequest


class PreviewCollector:
    def __init__(self, backend, request: PreviewRequest):
        self.backend, self.request = backend, request
        self.audio = bytearray()
        self.rate = 0
        self.text = ""
        self.done = asyncio.Event()
        self.error: Literal["", "invalid_audio", "unavailable"] = ""

    async def handle(self, message):
        if "setupComplete" in message:
            self.backend._ready.set()
        server = message.get("serverContent") or {}
        self.text += (server.get("outputTranscription") or {}).get("text") or ""
        for part in (server.get("modelTurn") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if inline.get("data"):
                try:
                    mime = inline.get("mimeType") or inline.get("mime_type") or ""
                    rate = int(mime.split("rate=")[1].split(";")[0]) if "rate=" in mime else 24000
                    pcm = base64.b64decode(inline["data"], validate=True)
                    if (not 8000 <= rate <= 96000 or (self.rate and self.rate != rate)
                            or len(pcm) % 2 or len(self.audio) + len(pcm) > rate * 2 * 20):
                        raise ValueError("Invalid preview audio")
                    self.rate = rate
                    self.audio.extend(pcm)
                except (ValueError, TypeError):
                    self.error = "invalid_audio"
                    self.done.set()
        if server.get("interrupted"):
            self.error = "unavailable"
            self.done.set()
        elif server.get("turnComplete"):
            self.done.set()

    def transport_lost(self):
        self.error = "unavailable"
        self.done.set()


async def generate_preview(backend, request: PreviewRequest):
    if not backend.capabilities().live:
        actual = (request.text if request.kind == "voice" else
                  await backend.rewrite_announcement(request.kind, request.text))
        pcm, rate = await backend.tts(actual)
    else:
        collector = PreviewCollector(backend, request)
        backend._preview_collector = collector
        await backend.start_session()
        from dialogue._internal.announcements import announcement_instruction
        text = (request.text if request.kind == "voice" else
                announcement_instruction(request.kind, request.text))
        await backend._send({"realtimeInput": {"text": text}})
        await collector.done.wait()
        if collector.error:
            return PreviewFailure(request.preview_id, collector.error)
        pcm, rate, actual = bytes(collector.audio), collector.rate, collector.text or request.text
    if not pcm:
        return PreviewFailure(request.preview_id, "no_audio")
    if (not 8000 <= rate <= 96000 or len(pcm) % 2 or len(pcm) > rate * 2 * 20):
        return PreviewFailure(request.preview_id, "invalid_audio")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm)
    return PreviewArtifact(request.preview_id, actual, output.getvalue(), rate, len(pcm)/(rate * 2))


class PreviewService:
    def __init__(self, backend_factory=None, voice_library: VoiceLibrary | None = None):
        self._lock = asyncio.Lock()
        self._factory = backend_factory
        self._voice_library = voice_library

    @property
    def busy(self):
        return self._lock.locked()

    async def preview(self, settings, request: PreviewRequest):
        if self.busy:
            return PreviewFailure(request.preview_id, "busy")
        async with self._lock:
            from dialogue import create_backend, create_voice_library
            fresh = copy.deepcopy(settings)
            if request.model:
                if fresh.backend != "aliyun_deepseek" or fresh.aliyun_tts_model != request.model:
                    raise ValueError("语音后端或模型已改变，请刷新音色列表后再试听")
                voice = await (self._voice_library or create_voice_library()).check(fresh, request.voice)
                if voice.status != "OK":
                    raise ValueError("音色尚未就绪，请稍后刷新列表")
            fresh.voice_leave_enabled = fresh.voice_volume_enabled = False
            fresh.persona = request.persona or fresh.persona
            if request.voice:
                fresh.mimo_tts_voice = fresh.gemini_voice = fresh.aliyun_tts_voice = request.voice
            backend = (self._factory(fresh) if self._factory else
                       create_backend(fresh))
            try:
                return await asyncio.wait_for(generate_preview(backend, request), 30)
            except asyncio.TimeoutError:
                return PreviewFailure(request.preview_id, "timeout")
            except asyncio.CancelledError:
                raise
            except Exception:
                return PreviewFailure(request.preview_id, "unavailable")
            finally:
                cleanup = asyncio.create_task(backend.aclose())
                try:
                    await asyncio.wait_for(asyncio.shield(cleanup), 3)
                except asyncio.TimeoutError:
                    cleanup.cancel()
                    await asyncio.gather(cleanup, return_exceptions=True)

    async def generate(self, settings, kind, text, voice="", model: str = ""):
        """现有HTTP响应适配；WAV仅在此出口编码。"""
        request = PreviewRequest(uuid4().hex, kind, text.strip(), voice.strip(), settings.persona, model)
        result = await self.preview(settings, request)
        if isinstance(result, PreviewFailure):
            raise ValueError(f"试听生成失败：{result.reason}")
        return {"text": result.text, "wav_base64": base64.b64encode(result.wav_bytes).decode(),
                "sample_rate": result.sample_rate, "duration_seconds": result.duration_seconds}
