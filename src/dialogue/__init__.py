"""生成能力工厂与诊断/音频工具；导入不创建网络连接。"""

from collections.abc import Awaitable, Callable

from channels.models import SessionRef
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
from dialogue._internal.reconfiguration import requires_session_restart
from dialogue._internal.ws_transport import _AiohttpWs, open_live_ws
from dialogue.api import (
    DialogueSettings,
    ExpressionJob,
    GeneratedAudio,
    PreviewService,
    VoiceBackend,
)

__all__ = [
    "VoiceBackend",
    "adapt_websocket",
    "announcement_instruction",
    "audio_output",
    "build_live_url",
    "clamp_reply",
    "clean_announcement",
    "create_aliyun_backend",
    "create_backend",
    "create_expression_job",
    "create_gemini_backend",
    "create_mimo_backend",
    "create_openai_backend",
    "create_preview_service",
    "live_url_has_key",
    "looks_like_reasoning_leak",
    "open_live_ws",
    "pcm16_to_wav",
    "requires_session_restart",
    "resample_pcm16",
    "sanitize_asr",
    "sanitize_for_tts",
    "wav_to_pcm16",
]


def create_mimo_backend(settings: DialogueSettings) -> VoiceBackend:
    from dialogue._internal.cascade.mimo_cascade import MimoCascadeBackend
    return MimoCascadeBackend(settings)


def create_aliyun_backend(settings: DialogueSettings) -> VoiceBackend:
    from dialogue._internal.cascade.aliyun_deepseek import AliyunDeepSeekBackend
    return AliyunDeepSeekBackend(settings)


def create_gemini_backend(settings: DialogueSettings, limits=None, connector=None) -> VoiceBackend:
    from dialogue._internal.live.gemini_live import GeminiLiveBackend
    return GeminiLiveBackend(settings, limits=limits,
                             connector=connector)


def create_openai_backend(settings: DialogueSettings, limits=None, connector=None) -> VoiceBackend:
    from dialogue._internal.live.gemini_live import OpenAiRealtimeBackend
    return OpenAiRealtimeBackend(settings, limits=limits,
                                 connector=connector)


def create_backend(settings: DialogueSettings) -> VoiceBackend:
    """按已解析配置装配生成适配；历史与工具在每任务/会话显式绑定。"""
    backend = (settings.backend or "").strip().lower()
    if backend == "aliyun_deepseek":
        return create_aliyun_backend(settings)
    if backend in {"gemini", "gemini_live", "live", "live_gemini", "speech_to_speech"}:
        return create_gemini_backend(settings)
    if backend in {"openai", "openai_realtime", "realtime"}:
        return create_openai_backend(settings)
    if backend in {"mimo", "mimo_cascade", "cascade", "cascade_mimo"}:
        return create_mimo_backend(settings)
    raise ValueError(f"unknown voice backend: {settings.backend}")


def adapt_websocket(ws, session=None):
    """把已建立的 aiohttp WebSocket 和其会话装配为生成传输；close 负责释放两者。"""
    return _AiohttpWs(ws, session)


def create_expression_job(action_id: str, session: SessionRef, producer: Callable[..., Awaitable[None]], *, deadline: float, queue_size: int = 16) -> ExpressionJob:
    """受控表达任务；主要由后端 generate 使用，亦供协议适配测试注入生产者。"""
    from dialogue._internal.output import ExpressionJob as Job
    return Job(action_id, session, producer, deadline=deadline, queue_size=queue_size)


def audio_output(job: ExpressionJob) -> GeneratedAudio:
    """单消费者输出适配；evidence不消费上游，也不代表已播放。"""
    from dialogue._internal.output import GeneratedAudio as Audio
    return Audio(job)


def create_preview_service(*, backend_factory=None) -> PreviewService:
    """临时试听供应商工厂；不读取真实房间历史，不创建任何渠道会话。"""
    from dialogue._internal.preview import PreviewService as Service
    return Service(backend_factory)
