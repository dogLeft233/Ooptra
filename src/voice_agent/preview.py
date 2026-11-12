"""Disposable model sessions produce browser WAV previews, never room audio."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import io
import wave

from behavior import create_reply_policy
from dialogue import create_backend
from operations.models import VoiceAgentSettings


class EphemeralMemory:
    def append(self, *args, **kwargs):
        return {}

    def as_messages(self, **kwargs):
        return []

    def recent(self, **kwargs):
        return []


def saved_prompts(config, kind: str, area: str = "") -> list[str]:
    from behavior import effective_area
    if kind not in {"enter", "leave"}:
        return []
    policy = effective_area(config, area) if area else config.defaults
    return list(policy[kind + "_prompts"])


class PreviewService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def generate(self, settings: VoiceAgentSettings, kind: str, text: str,
                       voice: str = "") -> dict:
        if kind not in {"voice", "enter", "leave"}:
            raise ValueError("试听类型无效")
        text, voice = str(text).strip(), str(voice).strip()
        if not text or len(text) > 500 or len(voice) > 80:
            raise ValueError("试听文本需为 1~500 字，音色名称不能超过 80 字")
        if self.busy:
            raise ValueError("正在生成试听，请稍后重试")
        fresh = dataclasses.replace(settings)
        if voice:
            fresh.mimo_tts_voice = fresh.gemini_voice = fresh.aliyun_tts_voice = voice
        backend = create_backend(fresh, EphemeralMemory(), reply_policy=create_reply_policy())
        async with self._lock:
            try:
                return await asyncio.wait_for(self._generate(backend, fresh, kind, text), 30)
            finally:
                # Shield cleanup from browser/request cancellation, with a finite bound.
                cleanup = asyncio.create_task(backend.aclose())
                try:
                    await asyncio.wait_for(asyncio.shield(cleanup), 3)
                except asyncio.TimeoutError:
                    cleanup.cancel()
                    await asyncio.gather(cleanup, return_exceptions=True)

    async def _generate(self, backend, settings, kind: str, text: str) -> dict:
        if not backend.capabilities().live:
            actual = text if kind == "voice" else await backend.rewrite_announcement(kind, text)
            pcm, rate = await backend.tts(actual)
        else:
            done = asyncio.Event()
            audio = bytearray()
            transcript = []
            rates = []
            def on_audio(pcm, rate):
                if rates and rates[0] != rate:
                    raise ValueError("试听采样率发生变化，请重试")
                rates.append(rate)
                if len(audio) + len(pcm) > rate * 2 * 20:
                    raise ValueError("试听音频超过 20 秒")
                audio.extend(pcm)
            backend.set_audio_out(on_audio)
            backend.set_text_out(lambda value: transcript.append(value))
            backend.set_turn_out(lambda interrupted: done.set())
            if kind == "voice":
                await backend.speak_text(text)
            else:
                await backend.speak_announcement(kind, text)
            await done.wait()
            pcm, rate = bytes(audio), rates[0] if rates else settings.sample_rate_out
            actual = "".join(transcript) or text
        if not pcm or not 8000 <= int(rate) <= 96000 or len(pcm) > int(rate) * 2 * 20:
            raise ValueError("模型未产生可用的短音频")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(int(rate))
            output.writeframes(pcm)
        return {"text": actual, "wav_base64": base64.b64encode(buffer.getvalue()).decode(),
                "sample_rate": int(rate), "duration_seconds": len(pcm) / (int(rate) * 2)}
