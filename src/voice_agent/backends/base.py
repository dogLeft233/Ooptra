from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from voice_agent.voice_control import VoiceControlDecision


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


@dataclass
class VoiceReply:
    user_text: str = ""
    text: str = ""  # assistant reply text
    pcm16: bytes = b""
    sample_rate: int = 24000
    user_key: str = ""
    raw: dict = field(default_factory=dict)


class VoiceStreamInterrupted(Exception):
    """Playback was interrupted or its room session was replaced."""


class VoiceStreamError(RuntimeError):
    """An audible generation failed; do not retry it as an ordinary chat turn."""


AudioSink = Callable[[bytes, int], Awaitable[None]]


@runtime_checkable
class StreamingUtterance(Protocol):
    async def handle_utterance_stream(
        self, pcm16: bytes, *, sample_rate: int, user_key: str, channel_key: str,
        on_audio: AudioSink,
    ) -> VoiceReply: ...


class VoiceBackend(ABC):
    """语音大脑：输入一段 utterance PCM，输出文字 + 回话 PCM。"""

    name: str = "base"
    context_bridge: Any = None
    settings: Any
    _bridge_output_failed: bool = False

    @property
    def effective_persona(self) -> str:
        bridge = self.context_bridge
        return bridge.persona if bridge is not None and bridge.active else self.settings.persona

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
        return None
