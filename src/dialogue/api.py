from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from behavior.models import VoiceControlDecision, VolumeCommand
from dialogue.models import (
    AudioSink,
    DialogueCapabilities,
    GeneratedTurn,
    RecognitionSink,
    VoiceReply,
)


class DialogueSettings(Protocol):
    """生成读取的配置视图；由装配传入，不依赖配置加载器或配置文件。"""

    @property
    def aliyun_asr_model(self) -> str: ...

    @property
    def aliyun_tts_model(self) -> str: ...

    @property
    def aliyun_tts_voice(self) -> str: ...

    @property
    def backend(self) -> str: ...

    @property
    def conversation_window_enabled(self) -> bool: ...

    @property
    def conversation_window_seconds(self) -> int: ...

    @property
    def dashscope_api_key(self) -> str: ...

    @property
    def dashscope_ws_url(self) -> str: ...

    @property
    def deepseek_api_key(self) -> str: ...

    @property
    def deepseek_base_url(self) -> str: ...

    @property
    def deepseek_model(self) -> str: ...

    @property
    def force_reply_keywords(self) -> list[str]: ...

    @property
    def gemini_api_key(self) -> str: ...

    @property
    def gemini_base_url(self) -> str: ...

    @property
    def gemini_model(self) -> str: ...

    @property
    def gemini_voice(self) -> str: ...

    @property
    def memory_max_turns(self) -> int: ...

    @property
    def mimo_api_key(self) -> str: ...

    @property
    def mimo_asr_model(self) -> str: ...

    @property
    def mimo_base_url(self) -> str: ...

    @property
    def mimo_llm_model(self) -> str: ...

    @property
    def mimo_tts_model(self) -> str: ...

    @property
    def mimo_tts_voice(self) -> str: ...

    @property
    def min_utterance_ms(self) -> int: ...

    @property
    def persona(self) -> str: ...

    @property
    def proxy(self) -> str: ...

    @property
    def reply_probability_percent(self) -> int: ...

    @property
    def sample_rate_out(self) -> int: ...

    @property
    def voice_leave_enabled(self) -> bool: ...

    @property
    def voice_volume_enabled(self) -> bool: ...


class HistoryReader(Protocol):
    """读取已确认的近期历史；生成模块没有追加/清空权威历史的权限。"""

    def as_messages(self, *, user_key: str = "", limit: int | None = None) -> list[dict[str, str]]: ...


class MemoryTools(Protocol):
    """应用注入的共享上下文读取与授权工具端口；不暴露 outbox/pending。"""

    @property
    def active(self) -> bool: ...
    @property
    def persona(self) -> str: ...
    @property
    def context(self) -> dict[str, Any]: ...
    @property
    def history(self) -> list[dict[str, str]]: ...
    @property
    def session_id(self) -> str: ...
    async def tool(self, name: str, args: Any, *, speaker: str = "") -> dict: ...


class ReplyPolicyPort(Protocol):
    """行为注入的准入判断；生成不创建策略状态或修改额度。"""

    def select(self, percent: int) -> bool: ...
    def matches_keywords(self, text: str, keywords: list[str]) -> bool: ...
    def observe(self, text: str, keywords: list[str], user_key: str, seconds: float) -> bool: ...
    def clear(self) -> None: ...


@runtime_checkable
class SessionStarter(Protocol):
    """Live-only capability; cascade backends do not own a session."""

    async def start_session(self) -> None: ...


@runtime_checkable
class ConnectionReporter(Protocol):
    def connection_status(self) -> dict[str, object]: ...


@runtime_checkable
class ControlIntentDetector(Protocol):
    """Standalone intent classification, available on cascade backends."""

    async def detect_control_intent(self, user_text: str) -> VoiceControlDecision: ...


@runtime_checkable
class StreamingUtterance(Protocol):
    """逐输入流式生成；同实例串行，等待期间可取消，不复用上一请求的音频sink。"""

    async def handle_utterance_stream(
        self, pcm16: bytes, *, sample_rate: int, user_key: str, channel_key: str,
        on_audio: AudioSink, on_recognition: RecognitionSink | None = None,
    ) -> VoiceReply: ...


@runtime_checkable
class LiveDialogue(SessionStarter, ConnectionReporter, Protocol):
    """持续模型会话；输出回调只交付生成证据，实际播放/历史提交由应用负责。

    回调应及时返回；GeneratedTurn 包含普通回复与内部公告的文字及生成终态，回调返回前不得清除该证据。耗时的播放确认由应用拥有的有界任务调用渠道 Playback 处理。aclose 撤销供应商
    连接和工具任务，不替应用关闭渠道或提交历史。完整提议/许可协议仍在后续迁移。
    """

    def set_audio_out(self, handler: Callable[[bytes, int], Awaitable[None] | None] | None) -> None: ...
    def set_text_out(self, handler: Callable[[str], Awaitable[None] | None] | None) -> None: ...
    def set_turn_out(self, handler: Callable[[bool], Awaitable[None] | None] | None) -> None: ...
    def set_generated_out(self, handler: Callable[[GeneratedTurn], Awaitable[None] | None] | None) -> None: ...
    def set_voice_volume_out(self, handler: Callable[[VolumeCommand], Awaitable[dict]] | None) -> None: ...
    def set_voice_leave_out(self, handler: Callable[[], bool] | None) -> None: ...
    def note_input_speaker(self, uid: str) -> None: ...
    async def push_audio(self, pcm16: bytes, sample_rate: int | None = None) -> None: ...
    async def speak_text(self, text: str) -> None: ...
    async def speak_announcement(self, kind: str, template: str) -> None: ...
    async def reset_reply_session(self) -> None: ...
    async def aclose(self) -> None: ...


class ObservationReader(Protocol):
    def snapshot(self) -> dict: ...


class VoiceBackend(ABC):
    """语音大脑：输入一段 utterance PCM，输出文字 + 回话 PCM。"""

    name: str = "base"
    context_bridge: MemoryTools | None = None
    settings: DialogueSettings

    @property
    @abstractmethod
    def observation(self) -> ObservationReader: ...

    def capabilities(self) -> DialogueCapabilities:
        """声明可调用能力，不由调用方猜测供应商名。"""
        return DialogueCapabilities()

    @property
    def effective_persona(self) -> str:
        bridge = self.context_bridge
        return bridge.persona if bridge is not None and bridge.active else self.settings.persona

    @abstractmethod
    def bind_history(self, reader: HistoryReader) -> None:
        """替换未来级联请求的历史读取端口；不重写当前请求已读取的快照。"""
        ...

    async def tts(self, text: str) -> tuple[bytes, int]:
        """级联的固定文字合成；Live 不承诺逐字合成，应使用其表达能力。"""
        raise NotImplementedError("Fixed speech is unsupported by this backend")

    @abstractmethod
    async def handle_utterance(
        self,
        pcm16: bytes,
        *,
        sample_rate: int,
        user_key: str = "",
        channel_key: str = "",
    ) -> VoiceReply:
        raise NotImplementedError

    async def aclose(self) -> None:
        """停止本实例的生成与供应商资源；不触碰渠道房间或写历史。"""
        return None
