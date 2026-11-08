"""生成结果与流终态；生成完成不表示渠道播放完成。"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
    streaming_recognition: bool = False
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
    finalize_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.status not in {"recognized", "no_speech", "failed"}:
            raise ValueError("未知识别结果")
        if self.status == "recognized" and not self.text.strip():
            raise ValueError("成功识别须有文字")


@dataclass(frozen=True)
class TranscriptRevision:
    """同sentence_id覆盖显示；只有final可供决策和持久历史。"""
    sentence_id: int
    text: str
    final: bool


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
    limits: LiveLimits = LiveLimits()

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
    kind: Literal["proposed", "updated", "output_ready", "turn_finished", "control_proposed", "control_cancelled"]
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


@dataclass(frozen=True)
class VoiceSample:
    """上传样本；WAV 在本地检查规格，压缩音频由供应商校验声学规格。"""

    prefix: str
    mime_type: str
    audio: bytes = field(repr=False)

    def __post_init__(self) -> None:
        import io
        import re
        import wave

        if not isinstance(self.prefix, str) or not re.fullmatch(r"[A-Za-z0-9]{1,10}", self.prefix):
            raise ValueError("音色名称须为 1到10 个英文字母或数字")
        if self.mime_type not in {"audio/wav", "audio/mpeg", "audio/mp4"}:
            raise ValueError("参考音频仅支持 WAV、MP3、M4A")
        if not isinstance(self.audio, bytes) or not 0 < len(self.audio) <= 10 * 1024 * 1024:
            raise ValueError("参考音频不能为空，且不得超过 10 MB")
        if self.mime_type == "audio/wav":
            try:
                with wave.open(io.BytesIO(self.audio)) as wav:
                    if (wav.getsampwidth() != 2 or wav.getframerate() < 16000
                            or wav.getnchannels() not in {1, 2}
                            or not 5 <= wav.getnframes() / wav.getframerate() <= 60):
                        raise ValueError("WAV 需要 16-bit、至少 16 kHz、1到2 声道，时长 5到60 秒")
                    expected = wav.getnframes() * wav.getnchannels() * 2
                    if len(wav.readframes(wav.getnframes())) != expected:
                        raise ValueError("WAV 音频数据不完整")
            except (wave.Error, EOFError) as exc:
                raise ValueError("无法读取 WAV 音频") from exc


@dataclass(frozen=True)
class ManagedVoice:
    voice_id: str
    target_model: str
    status: str
    kind: Literal["system", "custom"] = "custom"


@dataclass(frozen=True)
class VoicePage:
    voices: tuple[ManagedVoice, ...]
    page: int
    has_more: bool
