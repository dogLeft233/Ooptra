"""生成结果与流终态；生成完成不表示渠道播放完成。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from behavior.models import VoiceControlDecision


@dataclass(frozen=True)
class VoiceReply:
    user_text: str = ""
    text: str = ""  # assistant reply text
    pcm16: bytes = b""
    sample_rate: int = 24000
    user_key: str = ""
    control: VoiceControlDecision | None = None


class VoiceStreamInterrupted(Exception):
    """Playback was interrupted or its room session was replaced."""


class VoiceStreamError(RuntimeError):
    """An audible generation failed; do not retry it as an ordinary chat turn."""


class RecognizedGenerationFailed(VoiceStreamError):
    """生成未完成，但本轮识别已完成；不携带可提交的助手草稿。"""

    def __init__(self, user_text: str, reason: str = "Voice generation failed after recognition") -> None:
        super().__init__(reason)
        self.user_text = user_text


AudioSink = Callable[[bytes, int], Awaitable[None]]
RecognitionSink = Callable[[str], None]


@dataclass(frozen=True)
class LiveLimits:
    """实时迟到关联窗口与候选输出内存上限；不改变已生成回合的期限。"""
    late_transcription_seconds: float = 2.0
    deferred_audio_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.late_transcription_seconds < 0 or self.deferred_audio_bytes < 1:
            raise ValueError("Invalid Live limits")


@dataclass(frozen=True)
class GeneratedTurn:
    """模型回合证据。text 是草稿，selected 不代表已播放；持久化由应用核实执行结果。"""
    user_text: str = ""
    text: str = ""
    user_key: str = ""
    selected: bool = False
    interrupted: bool = False
    internal: bool = False


@dataclass(frozen=True)
class DialogueCapabilities:
    live: bool = False
    streaming_utterance: bool = False
    fixed_speech: bool = True
