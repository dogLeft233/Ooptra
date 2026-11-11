"""生成结果与流终态；生成完成不表示渠道播放完成。"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from behavior.models import SpeechIntent, VoiceControlDecision
from channels.models import ActionOutcome, PlaybackResult, SessionRef
from context.models import PreparedContext

if TYPE_CHECKING:
    from dialogue.api import ExpressionJob


class VoiceStreamInterrupted(Exception):
    """准备来源已失效；生成失败由输出适配转换为渠道源错误。"""


AudioSink = Callable[[bytes, int], Awaitable[None]]


@dataclass(frozen=True)
class LiveLimits:
    """实时迟到关联窗口与候选输出内存上限；不改变已生成回合的期限。"""
    late_transcription_seconds: float = 2.0
    deferred_audio_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if (type(self.late_transcription_seconds) not in {int, float}
                or not math.isfinite(self.late_transcription_seconds)
                or not 0 < self.late_transcription_seconds <= 30
                or type(self.deferred_audio_bytes) is not int or self.deferred_audio_bytes < 1):
            raise ValueError("Invalid Live limits")


@dataclass(frozen=True)
class DialogueCapabilities:
    live: bool = False
    streaming_utterance: bool = False
    fixed_speech: bool = True


@dataclass(frozen=True)
class AudioInput:
    input_ref: str
    session: SessionRef
    pcm16: bytes
    sample_rate: int
    user_key: str = ""
    attempt_id: str = ""

    def __post_init__(self) -> None:
        if (not self.input_ref or not isinstance(self.session, SessionRef)
                or not isinstance(self.pcm16, bytes)):
            raise ValueError("音频须有输入身份及不可变PCM")
        if type(self.sample_rate) is not int or not 8000 <= self.sample_rate <= 96000:
            raise ValueError("音频采样率须为8-96kHz")
        if len(self.pcm16) % 2 or len(self.pcm16) > self.sample_rate * 2 * 60:
            raise ValueError("音频须采样对齐且不超过60秒")


@dataclass(frozen=True)
class Recognition:
    input_ref: str
    session: SessionRef
    attempt_id: str
    user_key: str
    status: Literal["recognized", "no_speech", "failed"]
    text: str = ""
    reason: str = ""

    elapsed_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.status not in {"recognized", "no_speech", "failed"}:
            raise ValueError("未知识别结果")
        if self.status == "recognized" and not self.text.strip():
            raise ValueError("成功识别须有文字")


@dataclass(frozen=True)
class ControlInput:
    text: str
    input_ref: str
    session: SessionRef
    user_key: str = ""
    attempt_id: str = ""


@dataclass(frozen=True)
class ControlInterpretation:
    status: Literal["candidate", "invalid", "unavailable"]
    candidate: VoiceControlDecision | None = None
    reason: str = ""

    elapsed_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.status not in {"candidate", "invalid", "unavailable"}:
            raise ValueError("未知控制解释结果")
        if (self.status == "candidate") != isinstance(self.candidate, VoiceControlDecision):
            raise ValueError("控制解释必须保留解析候选，失败不能伪作NoControl")


@dataclass(frozen=True)
class GenerationEvent:
    """单任务序号；completed/failed 是唯一生成终态，不表示渠道完成。"""
    generation_id: str
    action_id: str
    session: SessionRef
    event_seq: int
    kind: Literal["text_delta", "text_final", "audio", "timing", "completed", "failed"]
    text: str = ""
    pcm16: bytes = b""
    sample_rate: int = 24000
    reason: str = ""

    timings_ms: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if (not self.generation_id or not self.action_id or self.event_seq < 0
                or self.kind not in {"text_delta", "text_final", "audio", "timing", "completed", "failed"}):
            raise ValueError("生成事件须含有效身份、序号和具名种类")
        if self.kind == "audio" and (not self.pcm16 or len(self.pcm16) % 2
                                    or len(self.pcm16) > 65536):
            raise ValueError("音频事件须为不超过64KiB的完整PCM16采样")


@dataclass(frozen=True)
class GenerationEvidence:
    generation_id: str
    action_id: str
    session: SessionRef
    status: Literal["running", "completed", "failed", "cancelled"]
    last_event_seq: int = -1
    last_frame_seq: int = -1
    text: str = ""
    reason: str = ""
    timings_ms: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class LiveSessionRequest:
    session: SessionRef
    context: PreparedContext
    limits: LiveLimits | None = None

    def __post_init__(self) -> None:
        if self.context.context_ref.session != self.session:
            raise ValueError("Live会话与上下文绑定不一致")


@dataclass(frozen=True)
class ProposalRef:
    live_session_id: str
    turn_id: str
    revision: int


@dataclass(frozen=True)
class Allow:
    intent: SpeechIntent


@dataclass(frozen=True)
class Hold:
    until: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.until):
            raise ValueError("等待许可必须有有限期限")


@dataclass(frozen=True)
class Reject:
    reason: str


@dataclass(frozen=True)
class ResolveReceipt:
    status: Literal["applied", "duplicate", "stale", "expired", "conflict", "held", "rejected", "unavailable", "accepted"]
    action_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ToolRef:
    live_session_id: str
    connection_generation: int
    provider_tool_call_id: str
    trigger_id: str


@dataclass(frozen=True)
class ControlResolution:
    status: Literal["executed", "scheduled", "rejected", "not_executed", "unknown"]
    outcome: ActionOutcome | PlaybackResult | None = None
    volume: int | None = None
    muted: bool | None = None
    reason: str = ""


@dataclass(frozen=True)
class LiveEvent:
    kind: Literal["proposed", "updated", "input_finalized", "output_ready", "turn_finished", "control_proposed", "control_cancelled"]
    session: SessionRef
    proposal: ProposalRef | None = None
    text: str = ""
    user_key: str = ""
    deadline: float = 0.0
    action_id: str = ""
    job: ExpressionJob | None = None
    tool_ref: ToolRef | None = None
    candidate: VoiceControlDecision | None = None
    reason: str = ""
    internal: bool = False


@dataclass(frozen=True)
class LiveSnapshot:
    live_session_id: str
    session: SessionRef
    state: Literal["opening", "ready", "failed", "closed"]
    current_proposal: ProposalRef | None = None
    action_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class PreviewRequest:
    preview_id: str
    kind: Literal["voice", "enter", "leave"]
    text: str
    voice: str = ""
    persona: str = ""

    def __post_init__(self) -> None:
        if (not self.preview_id or self.kind not in {"voice", "enter", "leave"}
                or not 1 <= len(self.text.strip()) <= 500 or len(self.voice) > 80):
            raise ValueError("试听须有身份、合法类型、1-500字文本和至多80字音色")


@dataclass(frozen=True)
class PreviewArtifact:
    preview_id: str
    text: str
    wav_bytes: bytes
    sample_rate: int
    duration_seconds: float


@dataclass(frozen=True)
class PreviewFailure:
    preview_id: str
    reason: Literal["busy", "timeout", "unavailable", "invalid_audio", "no_audio"]


@dataclass(frozen=True)
class DiagnosticRequest:
    pcm16: bytes = b""
    sample_rate: int = 16000

    def __post_init__(self) -> None:
        if (type(self.pcm16) is not bytes or len(self.pcm16) % 2
                or not 8000 <= self.sample_rate <= 96000
                or len(self.pcm16) > self.sample_rate * 2 * 10):
            raise ValueError("自检样本须为不超过10秒的8-96kHz PCM16")


@dataclass(frozen=True)
class DiagnosticStage:
    name: Literal["network", "asr", "intent", "tts"]
    state: Literal["pass", "fail", "skipped"]
    elapsed_ms: int = 0
    reason: str = ""


@dataclass(frozen=True)
class DiagnosticResult:
    stages: tuple[DiagnosticStage, ...]
