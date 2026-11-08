"""渠道房间契约；不可变引用不等于当前会话仍有效。"""

from dataclasses import dataclass
from enum import Enum
from typing import Literal


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


@dataclass(frozen=True)
class LeaveRoom:
    action_id: str
    session: SessionRef
    source: Literal["manual", "auto", "system"] = "manual"

    def __post_init__(self) -> None:
        _require_command(self.action_id, self.source)
        if not isinstance(self.session, SessionRef):
            raise ValueError("退房需要有效会话引用")


ChannelCommand = JoinRoom | LeaveRoom


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
        if (type(self.energy_threshold) not in (int, float)
                or not math.isfinite(self.energy_threshold) or not 0 <= self.energy_threshold <= 1):
            raise ValueError("VAD 能量阈值必须是 0-1 的有限数值")
