"""模型自检独占临时资源，不借房间身份或业务工具授权。"""
from __future__ import annotations

import asyncio
import base64
import copy
import time

from dialogue._internal.preview import PreviewCollector, generate_preview
from dialogue.models import DiagnosticResult, DiagnosticStage, PreviewFailure, PreviewRequest


class _InputCollector(PreviewCollector):
    def __init__(self, backend):
        super().__init__(backend, PreviewRequest("diagnostic-input", "voice", "自检"))
        self.input_text = ""
        self.control = False

    async def handle(self, message):
        self.input_text += ((message.get("serverContent") or {}).get("inputTranscription") or {}).get("text") or ""
        for call in (message.get("toolCall") or {}).get("functionCalls") or []:
            if call.get("name") == "leave_voice_room":
                self.control = True
                self.done.set()
        await super().handle(message)


async def run_diagnostic(backend, request):
    stages = []
    async def stage(name, operation):
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(operation(), 20)
            stages.append(DiagnosticStage(name,"pass",int((time.monotonic()-started)*1000)))
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            stages.append(DiagnosticStage(name,"fail",int((time.monotonic()-started)*1000),"model_unavailable"))
            return None

    live = backend.capabilities().live
    if not request.pcm16:
        async def connection():
            if live:
                await backend.start_session()
            else:
                await backend.detect_control_intent("你好，请保持在语音房，不要退出，也不要调节音量")
        await stage("network",connection)
        return DiagnosticResult(tuple(stages))
    if not live:
        async def recognize():
            text = await backend.asr(request.pcm16,request.sample_rate)
            if not text.strip():
                raise ValueError("No speech")
            return text
        text = await stage("asr",recognize)
        if text is None:
            stages.append(DiagnosticStage("intent","skipped",reason="no_transcript"))
        else:
            await stage("intent",lambda: backend.detect_control_intent(text))
        async def synthesize():
            pcm,rate = await backend.tts("你好，语音自检成功。")
            if (not pcm or not 8000 <= rate <= 96000 or len(pcm)%2 or len(pcm)>rate*2*20):
                raise ValueError("Invalid audio")
        await stage("tts",synthesize)
        return DiagnosticResult(tuple(stages))

    collector = _InputCollector(backend)
    backend._preview_collector = collector
    async def recognize_live():
        await backend.start_session()
        await backend._send({"realtimeInput":{"audio":{
            "data":base64.b64encode(request.pcm16).decode(),
            "mime_type":f"audio/pcm;rate={request.sample_rate}"}}})
        await backend._send({"realtimeInput":{"audioStreamEnd":True}})
        await collector.done.wait()
        if not collector.input_text.strip():
            raise ValueError("No transcription")
    await stage("asr",recognize_live)
    stages.append(DiagnosticStage("intent","pass" if collector.control else "skipped",
                                  reason="tool_candidate_only"))
    await backend.close()
    # 输入转写与输出自检使用两个连接，迟到输入输出不能冒充TTS成功。
    settings = copy.deepcopy(backend.settings)
    settings.voice_leave_enabled = settings.voice_volume_enabled = False
    fresh = type(backend)(settings,connector=backend._connector)
    try:
        async def output():
            result = await generate_preview(fresh,PreviewRequest("diagnostic-output","voice","请用中文说：你好，语音自检成功。"))
            if isinstance(result,PreviewFailure):
                raise ValueError("No output")
        await stage("tts",output)
    finally:
        await asyncio.wait_for(fresh.aclose(),3)
    return DiagnosticResult(tuple(stages))
