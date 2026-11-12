"""渠道房间契约；不可变引用不等于当前会话仍有效。"""

from dataclasses import dataclass
from enum import Enum
from typing import Literal

UTTERANCE_ENERGY_THRESHOLD = .02
VOICE_CONTROL_MIN_MS = 250


class RoomState(Enum):
    IDLE = "idle"
    JOINING = "joining"
    JOINED = "joined"
    RETIRING = "retiring"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class RoomTarget:
    area: str
    channel: str

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and v.strip() for v in (self.area, self.channel)):
            raise ValueError("房间 area/channel 必须是非空字符串")


@dataclass(frozen=True)
class SessionRef:
    process_run_id: str
    binding_id: str
    session_id: str
    room_target: RoomTarget

    def __post_init__(self) -> None:
        if not all(
            isinstance(v, str) and v.strip()
            for v in (self.process_run_id, self.binding_id, self.session_id)
        ) or not isinstance(self.room_target, RoomTarget):
            raise ValueError("会话引用需要有效身份和房间目标")


@dataclass(frozen=True)
class JoinRoom:
    action_id: str
    target: RoomTarget
    expected_revision: int
    source: Literal["manual", "auto", "system"] = "manual"

    def __post_init__(self) -> None:
        _require_command(self.action_id, self.source)
        if (
            not isinstance(self.target, RoomTarget)
            or type(self.expected_revision) is not int
            or self.expected_revision < 0
        ):
            raise ValueError("进房需要有效目标和非负整数版本")


@dataclass(frozen=True)
class ChannelSnapshot:
    revision: int
    state: RoomState
    session: SessionRef | None = None
    confirmed: bool = False


@dataclass(frozen=True)
class ActionOutcome:
    action_id: str
    status: Literal["completed", "rejected", "failed", "cancelled"]
    session: SessionRef | None = None
    reason: str = ""
    rtc_channel: str = ""
    media: Literal["text", "room", "output"] | None = None
    message_id: str = ""
    target: RoomTarget | None = None
    parent_action_id: str = ""


@dataclass(frozen=True)
class LeaveRoom:
    action_id: str
    session: SessionRef
    source: Literal["manual", "auto", "system"] = "manual"

    def __post_init__(self) -> None:
        _require_command(self.action_id, self.source)
        if not isinstance(self.session, SessionRef):
            raise ValueError("退房需要有效会话引用")


@dataclass(frozen=True)
class SetOutput:
    action_id: str
    session: SessionRef
    volume: int | None = None
    muted: bool | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.action_id, str) or not self.action_id.strip()
                or not isinstance(self.session, SessionRef)
                or (self.volume is None and self.muted is None)
                or (self.volume is not None and (type(self.volume) is not int or not 10 <= self.volume <= 100))
                or (self.muted is not None and type(self.muted) is not bool)):
            raise ValueError("输出控制须绑定会话和有效音量/静音目标")




@dataclass(frozen=True)
class AudioPushResult:
    ok: bool
    reason: str = ""


def _require_command(action_id: str, source: str) -> None:
    if (
        not isinstance(action_id, str)
        or not action_id.strip()
        or not isinstance(source, str)
        or source not in {"manual", "auto", "system"}
    ):
        raise ValueError("动作需要非空标识及合法来源")


@dataclass(frozen=True)
class RunningAction:
    action_id: str
    status: Literal["running"] = "running"


@dataclass(frozen=True)
class UnknownAction:
    action_id: str
    status: Literal["unknown"] = "unknown"


ActionState = ActionOutcome | RunningAction | UnknownAction


@dataclass(frozen=True)
class PlatformMember:
    """平台成员事实；无法识别的机器人标记为 None，不能据此选作真人。"""

    uid: str
    is_bot: bool | None
    is_self: bool


@dataclass(frozen=True)
class RoomMembership:
    target: RoomTarget
    members: tuple[PlatformMember, ...]


@dataclass(frozen=True)
class JoinedAreas:
    status: Literal["confirmed", "unavailable", "invalid_response"]
    area_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AreaMembership:
    area: str
    status: Literal["confirmed", "unavailable", "invalid_response"]
    rooms: tuple[RoomMembership, ...] = ()


@dataclass(frozen=True)
class RoomOperation:
    """实际成功进退房的通知；在执行锁释放后交付。"""

    kind: str
    source: str
    area: str
    channel: str
    visit_id: str


@dataclass(frozen=True)
class VadvConfig:
    sample_rate: int = 16000
    frame_ms: int = 30
    # 与 VoiceAgentSettings.silence_ms 保持一致：700ms 对中文偏短，会把一句话切碎。
    silence_ms: int = 1200
    max_utterance_ms: int = 15000
    energy_threshold: float = 0.012  # 归一化 RMS 阈值，可按环境调


    def __post_init__(self) -> None:
        import math
        for value in (self.sample_rate, self.frame_ms, self.silence_ms, self.max_utterance_ms):
            if type(value) is not int or value <= 0:
                raise ValueError("VAD 采样率及窗口必须为正整数")
        if not 8000 <= self.sample_rate <= 96000 or self.frame_ms > self.max_utterance_ms:
            raise ValueError("VAD采样率须为8-96kHz，一帧不能超过整句最大预算")
        if (type(self.energy_threshold) not in (int, float)
                or not math.isfinite(self.energy_threshold) or not 0 <= self.energy_threshold <= 1):
            raise ValueError("VAD 能量阈值必须是 0-1 的有限数值")


@dataclass(frozen=True)
class OutputFrame:
    action_id: str
    session: SessionRef
    output_id: str
    frame_seq: int
    pcm16: bytes
    sample_rate: int
    channels: int = 1

    def __post_init__(self) -> None:
        if (not isinstance(self.action_id, str) or not self.action_id.strip()
                or not isinstance(self.output_id, str) or not self.output_id.strip()
                or not isinstance(self.session, SessionRef)
                or type(self.frame_seq) is not int or self.frame_seq < 0
                or type(self.pcm16) is not bytes or not self.pcm16
                or len(self.pcm16) % 2 or len(self.pcm16) > 65536
                or type(self.sample_rate) is not int or not 8000 <= self.sample_rate <= 96000
                or type(self.channels) is not int or self.channels != 1):
            raise ValueError("输出帧必须携带身份、连续序号及不超过64KiB的单声道PCM16")


@dataclass(frozen=True)
class PlaybackRequest:
    action_id: str
    session: SessionRef
    output_id: str
    deadline: float

    def __post_init__(self) -> None:
        import math
        if (not isinstance(self.action_id, str) or not self.action_id.strip()
                or not isinstance(self.output_id, str) or not self.output_id.strip()
                or not isinstance(self.session, SessionRef)
                or type(self.deadline) not in {int, float} or not math.isfinite(self.deadline)):
            raise ValueError("播放请求须有动作、会话、输出标识与有限单调时钟截止")


@dataclass(frozen=True)
class PlaybackResult:
    action_id: str
    session: SessionRef
    output_id: str
    status: Literal["completed", "rejected", "expired", "cancelled", "failed", "partial"]
    reason: str = ""
    submitted_frames: int = 0
    submitted_samples: int = 0
    confirmed_samples: int = 0
    unknown_samples: int = 0

    def __post_init__(self) -> None:
        if (not isinstance(self.action_id, str) or not self.action_id.strip()
                or not isinstance(self.output_id, str) or not self.output_id.strip()
                or not isinstance(self.session, SessionRef)
                or not isinstance(self.status, str)
                or self.status not in {"completed", "rejected", "expired", "cancelled", "failed", "partial"}
                or any(type(value) is not int or value < 0 for value in
                       (self.submitted_frames, self.submitted_samples, self.confirmed_samples, self.unknown_samples))
                or self.confirmed_samples + self.unknown_samples != self.submitted_samples
                or bool(self.submitted_frames) != bool(self.submitted_samples)
                or (self.status == "completed" and (not self.submitted_frames or self.unknown_samples))):
            raise ValueError("播放证据需要合法身份、非负整数范围及一致的实际确认；完成不能含未知样本")


PlaybackState = PlaybackResult | RunningAction | UnknownAction


class OutputSourceError(RuntimeError):
    """生成源未正常完成；消费者不得将失败视为正常EOS。"""


@dataclass(frozen=True)
class AudioInputPolicy:
    vad: VadvConfig = VadvConfig()
    listen_only_uids: tuple[str, ...] = ()
    min_utterance_ms: int = 900
    energy_threshold: float = 0.02

    def __post_init__(self) -> None:
        import math
        if (not isinstance(self.vad, VadvConfig) or type(self.listen_only_uids) is not tuple
                or any(type(uid) is not str or not uid for uid in self.listen_only_uids)
                or type(self.min_utterance_ms) is not int or self.min_utterance_ms < 0
                or type(self.energy_threshold) not in {int, float} or not math.isfinite(self.energy_threshold)
                or not 0 <= self.energy_threshold <= 1):
            raise ValueError("音频输入策略需要有效VAD、白名单、最小时长及能量门限")


@dataclass(frozen=True)
class IncomingAudio:
    user_key: str
    pcm16: bytes
    sample_rate: int
    sequence: int

    def __post_init__(self) -> None:
        if (type(self.user_key) is not str or not self.user_key or len(self.user_key) > 256
                or type(self.pcm16) is not bytes or not self.pcm16 or len(self.pcm16) % 2
                or len(self.pcm16) > 65536 or type(self.sample_rate) is not int
                or not 8000 <= self.sample_rate <= 96000 or type(self.sequence) is not int or self.sequence < 1):
            raise ValueError("输入帧需要来源、合法单声道PCM16、采样率与正序号")


@dataclass(frozen=True)
class UtteranceResult:
    status: Literal["ready", "accumulating", "noise", "too_short", "gap"]
    pcm16: bytes = b""

    def __post_init__(self) -> None:
        if (self.status not in {"ready", "accumulating", "noise", "too_short", "gap"}
                or type(self.pcm16) is not bytes or len(self.pcm16) % 2
                or bool(self.pcm16) != (self.status == "ready")):
            raise ValueError("只有完整有声切句可以携带PCM")


@dataclass(frozen=True)
class SendText:
    """独立文字动作；target必须由调用方显式解析，不从语音房猜目的地。"""
    action_id: str
    session: SessionRef
    target: RoomTarget
    text: str
    parent_action_id: str = ""

    def __post_init__(self) -> None:
        if (not isinstance(self.action_id, str) or not self.action_id.strip()
                or not isinstance(self.session, SessionRef) or not isinstance(self.target, RoomTarget)
                or not isinstance(self.text, str) or not self.text.strip()
                or not isinstance(self.parent_action_id, str) or self.parent_action_id == self.action_id):
            raise ValueError("文字动作需要身份、原会话、显式目标及非空文字；父动作不能是自身")


ChannelCommand = JoinRoom | LeaveRoom | SetOutput | SendText
