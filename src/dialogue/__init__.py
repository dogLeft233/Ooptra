"""生成能力工厂与诊断/音频工具；导入不创建网络连接。"""

from typing import TYPE_CHECKING

from dialogue._internal.announcements import announcement_instruction, clean_announcement
from dialogue._internal.cascade.mimo_cascade import (
    clamp_reply,
    looks_like_reasoning_leak,
    pcm16_to_wav,
    resample_pcm16,
    sanitize_asr,
    sanitize_for_tts,
    wav_to_pcm16,
)
from dialogue._internal.live.gemini_live import build_live_url, live_url_has_key
from dialogue._internal.ws_transport import _AiohttpWs, open_live_ws
from dialogue.api import DialogueSettings, HistoryReader, ReplyPolicyPort, VoiceBackend

if TYPE_CHECKING:
    from dialogue._internal.cascade.aliyun_deepseek import AliyunDeepSeekBackend
    from dialogue._internal.cascade.mimo_cascade import MimoCascadeBackend
    from dialogue._internal.live.gemini_live import GeminiLiveBackend, OpenAiRealtimeBackend

__all__ = [
    "VoiceBackend",
    "adapt_websocket",
    "announcement_instruction",
    "build_live_url",
    "clamp_reply",
    "clean_announcement",
    "create_aliyun_backend",
    "create_backend",
    "create_gemini_backend",
    "create_mimo_backend",
    "create_openai_backend",
    "live_url_has_key",
    "looks_like_reasoning_leak",
    "open_live_ws",
    "pcm16_to_wav",
    "resample_pcm16",
    "sanitize_asr",
    "sanitize_for_tts",
    "wav_to_pcm16",
]


def create_mimo_backend(settings: DialogueSettings, memory: HistoryReader, *, reply_policy: ReplyPolicyPort) -> "MimoCascadeBackend":
    from dialogue._internal.cascade.mimo_cascade import MimoCascadeBackend
    return MimoCascadeBackend(settings, memory, reply_policy=reply_policy)


def create_aliyun_backend(settings: DialogueSettings, memory: HistoryReader, *, reply_policy: ReplyPolicyPort) -> "AliyunDeepSeekBackend":
    from dialogue._internal.cascade.aliyun_deepseek import AliyunDeepSeekBackend
    return AliyunDeepSeekBackend(settings, memory, reply_policy=reply_policy)


def create_gemini_backend(settings: DialogueSettings, memory: HistoryReader, *, reply_policy: ReplyPolicyPort, limits=None, connector=None) -> "GeminiLiveBackend":
    from dialogue._internal.live.gemini_live import GeminiLiveBackend
    return GeminiLiveBackend(settings, memory, reply_policy=reply_policy, limits=limits,
                             connector=connector)


def create_openai_backend(settings: DialogueSettings, memory: HistoryReader, *, reply_policy: ReplyPolicyPort, limits=None, connector=None) -> "OpenAiRealtimeBackend":
    from dialogue._internal.live.gemini_live import OpenAiRealtimeBackend
    return OpenAiRealtimeBackend(settings, memory, reply_policy=reply_policy, limits=limits,
                                 connector=connector)


def create_backend(settings: DialogueSettings, memory: HistoryReader, *, reply_policy: ReplyPolicyPort) -> VoiceBackend:
    """装配模型实现。策略端口由应用注入；关闭和输出回调由调用方按 api 管理。

    当前保持级联与 Live 的迁移期能力接口；不声明完整 job/lease 协议已实现。
    """
    backend = (settings.backend or "").strip().lower()
    if backend == "aliyun_deepseek":
        return create_aliyun_backend(settings, memory, reply_policy=reply_policy)
    if backend in {"gemini", "gemini_live", "live", "live_gemini", "speech_to_speech"}:
        return create_gemini_backend(settings, memory, reply_policy=reply_policy)
    if backend in {"openai", "openai_realtime", "realtime"}:
        return create_openai_backend(settings, memory, reply_policy=reply_policy)
    if backend in {"mimo", "mimo_cascade", "cascade", "cascade_mimo"}:
        return create_mimo_backend(settings, memory, reply_policy=reply_policy)
    raise ValueError(f"unknown voice backend: {settings.backend}")


def adapt_websocket(ws, session=None):
    """把已建立的 aiohttp WebSocket 和其会话装配为生成传输；close 负责释放两者。"""
    return _AiohttpWs(ws, session)
