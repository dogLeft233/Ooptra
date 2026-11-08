"""行为模块的不可变结果；不包含传输或模型对象。"""

from dataclasses import dataclass
from typing import Any, Literal, cast

from channels.models import SessionRef
from context.models import ContextRevision


@dataclass(frozen=True)
class AttemptPermit:
    """已经消耗的级联输入尝试；生成失败或沉默不退款。"""

    session: SessionRef
    attempt_number: int
    input_ref: str
    expires_at: float
    policy_version: int = 1

    def __post_init__(self) -> None:
        import math
        if (type(self.session) is not SessionRef
                or type(self.attempt_number) is not int or self.attempt_number < 1
                or type(self.input_ref) is not str or not self.input_ref.strip()
                or type(self.policy_version) is not int or self.policy_version < 1
                or type(self.expires_at) not in {int, float} or not math.isfinite(self.expires_at)):
            raise ValueError("尝试许可需要原会话、输入、正次数/版本及有限期限")

    @property
    def attempt_id(self) -> str:
        identity = (self.session.process_run_id, self.session.binding_id, self.session.session_id,
                    self.session.room_target.area, self.session.room_target.channel)
        return "".join(f"{len(part)}:{part}" for part in identity) + f":{self.attempt_number}"


@dataclass(frozen=True)
class AttemptRejected:
    reason: Literal["stale_session", "limit_reached"]


@dataclass(frozen=True)
class AttemptsSnapshot:
    session: SessionRef | None
    attempts: int


def _readonly(*_args: Any, **_kwargs: Any) -> Any:
    raise TypeError("串门配置是只读快照；通过解析新配置并 reload 修改")


class _FrozenDict(dict[str, Any]):
    """保留 JSON/dict 兼容的只读值；嵌套值由 _freeze_config 先冻结。"""

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly

    def __deepcopy__(self, _memo: dict[int, object]) -> "_FrozenDict":
        return self


class _FrozenList(list[Any]):
    __setitem__ = __delitem__ = append = extend = insert = pop = remove = reverse = sort = _readonly
    __iadd__ = __imul__ = _readonly

    def __deepcopy__(self, _memo: dict[int, object]) -> "_FrozenList":
        return self


def _freeze_config(value: object) -> Any:
    if isinstance(value, dict):
        fields = cast("dict[object, object]", value)
        if any(not isinstance(key, str) for key in fields):
            raise ValueError("串门配置字段名须为字符串")
        return _FrozenDict((str(key), _freeze_config(item)) for key, item in fields.items())
    if isinstance(value, list):
        return _FrozenList(_freeze_config(item) for item in cast("list[object]", value))
    if isinstance(value, tuple):
        return tuple(_freeze_config(item) for item in cast("tuple[object, ...]", value))
    return value


@dataclass(frozen=True)
class AutoVisitConfig:
    check_interval_minutes: tuple[float, float]
    daily_limit: int
    defaults: dict[str, Any]
    areas: dict[str, Any]

    def __post_init__(self) -> None:
        if (type(self.check_interval_minutes) is not tuple
                or len(self.check_interval_minutes) != 2
                or any(type(item) not in {int, float} for item in self.check_interval_minutes)
                or not 1 <= self.check_interval_minutes[0] <= self.check_interval_minutes[1] <= 10080):
            raise ValueError("检查间隔必须为 1~10080 分钟的有序范围")
        if type(self.daily_limit) is not int or self.daily_limit < 0:
            raise ValueError("每日次数须为非负整数")
        if type(self.defaults) not in {dict, _FrozenDict} or type(self.areas) not in {dict, _FrozenDict}:
            raise ValueError("串门规则须通过 parse_auto_visit_config 解析")
        object.__setattr__(self, "defaults", _freeze_config(self.defaults))
        object.__setattr__(self, "areas", _freeze_config(self.areas))



VolumeAction = Literal["increase", "decrease", "set", "mute", "restore"]


def _parse_volume_action(raw: object) -> VolumeAction:
    if not isinstance(raw, str):
        raise ValueError("Invalid voice volume action")
    match raw:
        case "increase" | "decrease" | "set" | "mute" | "restore":
            return raw
        case _:
            raise ValueError("Invalid voice volume action")


def _command_fields(raw: object) -> dict[object, object]:
    if not isinstance(raw, dict):
        raise ValueError("Voice control input must be an object")
    # isinstance 已证明是字典；键和值仍是不可信 object，随后逐字段解析。
    return cast("dict[object, object]", raw)


@dataclass(frozen=True)
class VolumeCommand:
    action: VolumeAction
    value: int | None = None

    def __post_init__(self) -> None:
        action = _parse_volume_action(self.action)
        if action == "set":
            if type(self.value) is not int or not 10 <= self.value <= 100:
                raise ValueError("Volume must be an integer between 10 and 100")
        elif self.value is not None:
            raise ValueError("Only set accepts a volume value")


@dataclass(frozen=True)
class VoiceControlDecision:
    leave: bool = False
    volume: VolumeCommand | None = None

    def __post_init__(self) -> None:
        if type(self.leave) is not bool:
            raise ValueError("Leave must be a boolean")
        if self.volume is not None and type(self.volume) is not VolumeCommand:
            raise ValueError("Volume must be a parsed command")
        if self.leave and self.volume is not None:
            raise ValueError("Ambiguous voice control decision")


def parse_volume_command(raw: object) -> VolumeCommand | None:
    """保留解析结果；外部值既不强制转换，也不裁剪到合法范围。"""
    if raw is None:
        return None
    fields = _command_fields(raw)
    action = _parse_volume_action(fields.get("action"))
    if action == "set":
        value = fields.get("value")
        if set(fields) != {"action", "value"} or type(value) is not int:
            raise ValueError("Volume must be an integer between 10 and 100")
        return VolumeCommand(action, value)
    if set(fields) != {"action"}:
        raise ValueError("Invalid voice volume action or unexpected value")
    return VolumeCommand(action)


def parse_control_decision(raw: object) -> VoiceControlDecision:
    fields = _command_fields(raw)
    leave = fields.get("leave")
    if type(leave) is not bool or not set(fields) <= {"leave", "volume"}:
        raise ValueError("Invalid voice control decision")
    volume = parse_volume_command(fields.get("volume"))
    return VoiceControlDecision(leave=leave, volume=volume)



def resolve_volume_command(command: VolumeCommand, current: int) -> tuple[int | None, bool | None]:
    """把已解析的相对指令转换为渠道可执行的绝对音量/静音目标。"""
    if type(current) is not int or not 10 <= current <= 100:
        raise ValueError("当前音量必须为 10-100 的整数")
    if command.action == "increase":
        return min(100, current + 10), None
    if command.action == "decrease":
        return max(10, current - 10), None
    if command.action == "set":
        return command.value, None
    return None, command.action == "mute"


VOICE_CONTROL_MIN_MS = 250


@dataclass(frozen=True)
class ScheduledReply:
    """单槽调度选中的输入及代次；完成反馈不能影响其他代次。"""

    user_key: str
    pcm: bytes
    sample_rate: int
    offered_at: float
    generation: int
    sequence: int


@dataclass(frozen=True)
class SpeechActivity:
    """渠道确认的单帧音频活动；不携带供应商对象或原始PCM。"""

    user_key: str
    speaking: bool
    duration_ms: float

    def __post_init__(self) -> None:
        import math

        if type(self.user_key) is not str or not self.user_key:
            raise ValueError("音频活动须有来源标识")
        if type(self.speaking) is not bool:
            raise ValueError("音频活动须为明确布尔值")
        if (type(self.duration_ms) not in {int, float} or not math.isfinite(self.duration_ms)
                or self.duration_ms < 0):
            raise ValueError("音频活动时长须为非负有限毫秒数")


@dataclass(frozen=True)
class SpeechIntent:
    intent_id: str
    action_id: str
    session: SessionRef
    trigger_id: str
    context_version: ContextRevision
    deadline: float
    text: str = ""
    user_key: str = ""
    attempt_id: str = ""
    kind: Literal["reply", "fixed", "announcement"] = "reply"
    announcement_kind: Literal["enter", "leave"] = "enter"

    def __post_init__(self) -> None:
        import math
        if (any(type(value) is not str or not value.strip()
                for value in (self.intent_id, self.action_id, self.trigger_id))
                or any(type(value) is not str for value in (self.text, self.user_key, self.attempt_id))
                or type(self.announcement_kind) is not str or self.announcement_kind not in {"enter", "leave"}
                or (self.kind in {"fixed", "announcement"} and not self.text.strip())
                or type(self.session) is not SessionRef
                or type(self.context_version) is not ContextRevision
                or type(self.kind) is not str or self.kind not in {"reply", "fixed", "announcement"}
                or type(self.deadline) not in {int, float} or not math.isfinite(self.deadline)):
            raise ValueError("发言意图需要身份、会话、上下文版本及有限截止")


@dataclass(frozen=True)
class AudioReady:
    trigger_id: str
    session: SessionRef
    user_key: str
    context_version: ContextRevision

    def __post_init__(self) -> None:
        if (type(self.trigger_id) is not str or not self.trigger_id.strip()
                or type(self.session) is not SessionRef or type(self.user_key) is not str
                or type(self.context_version) is not ContextRevision):
            raise ValueError("输入触发需要身份和会话")


@dataclass(frozen=True)
class RecognitionObserved:
    trigger: AudioReady
    permit: AttemptPermit
    text: str
    context_version: ContextRevision
    audio_ms: float = 1000
    control: VoiceControlDecision | None = None

    def __post_init__(self) -> None:
        import math
        if (type(self.trigger) is not AudioReady or type(self.permit) is not AttemptPermit
                or self.permit.session != self.trigger.session
                or self.permit.input_ref != self.trigger.trigger_id
                or type(self.text) is not str or type(self.context_version) is not ContextRevision
                or type(self.audio_ms) not in {int, float} or not math.isfinite(self.audio_ms)
                or self.audio_ms < 0 or (self.control is not None and type(self.control) is not VoiceControlDecision)):
            raise ValueError("识别事实必须保留原输入许可、合法文字/控制与有限非负时长")


@dataclass(frozen=True)
class ReplySettings:
    probability: int = 100
    keywords: tuple[str, ...] = ()
    window_seconds: float = 0
    min_utterance_ms: int = 0
    lifetime_seconds: float = 60
    voice_leave_enabled: bool = False
    voice_volume_enabled: bool = False

    def __post_init__(self) -> None:
        import math
        if (type(self.probability) is not int or not 0 <= self.probability <= 100
                or type(self.keywords) is not tuple
                or any(type(word) is not str for word in self.keywords)
                or type(self.voice_leave_enabled) is not bool or type(self.voice_volume_enabled) is not bool
                or type(self.window_seconds) not in {int, float} or not math.isfinite(self.window_seconds)
                or self.window_seconds < 0
                or type(self.min_utterance_ms) is not int or self.min_utterance_ms < 0
                or type(self.lifetime_seconds) not in {int, float}
                or not math.isfinite(self.lifetime_seconds) or self.lifetime_seconds <= 0):
            raise ValueError("回应策略参数无效")


@dataclass(frozen=True)
class Speak:
    intent: SpeechIntent


@dataclass(frozen=True)
class NoAction:
    reason: str


@dataclass(frozen=True)
class ControlAction:
    action_id: str
    session: SessionRef
    control: VoiceControlDecision

    def __post_init__(self) -> None:
        if (type(self.action_id) is not str or not self.action_id.strip()
                or type(self.session) is not SessionRef or type(self.control) is not VoiceControlDecision
                or not (self.control.leave or self.control.volume is not None)):
            raise ValueError("控制动作需要合法身份、会话和实际候选")


@dataclass(frozen=True)
class AttemptFinished:
    permit: AttemptPermit
    end_reason: str
    handled_at: float


@dataclass(frozen=True)
class NotExecuted:
    action_id: str
    stage: str
    reason: str


@dataclass(frozen=True)
class FeedbackReceipt:
    status: Literal["accepted", "duplicate", "conflict", "unknown"]


@dataclass(frozen=True)
class LiveProposal:
    proposal_id: str
    session: SessionRef
    revision: int
    text: str
    user_key: str
    context_version: ContextRevision
    deadline: float
    internal: bool = False

    def __post_init__(self) -> None:
        import math
        if (type(self.proposal_id) is not str or not self.proposal_id.strip()
                or type(self.session) is not SessionRef
                or type(self.revision) is not int or self.revision < 0
                or type(self.text) is not str or type(self.user_key) is not str
                or type(self.context_version) is not ContextRevision
                or type(self.internal) is not bool
                or type(self.deadline) not in {int, float} or not math.isfinite(self.deadline)):
            raise ValueError("实时提议需要合法身份、版本、文字、显式内部标记和有限期限")


@dataclass(frozen=True)
class DeferLive:
    until: float


@dataclass(frozen=True)
class ControlProposal:
    trigger_id: str
    session: SessionRef
    candidate: VoiceControlDecision

    def __post_init__(self) -> None:
        if (type(self.trigger_id) is not str or not self.trigger_id.strip()
                or type(self.session) is not SessionRef or type(self.candidate) is not VoiceControlDecision):
            raise ValueError("控制提议需要触发身份、原会话和解析后的候选")
