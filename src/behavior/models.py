"""行为模块的不可变结果；不包含传输或模型对象。"""

from dataclasses import dataclass
from typing import Any, Literal, cast

from channels.models import SessionRef


@dataclass(frozen=True)
class AttemptPermit:
    """已经消耗的级联输入尝试；生成失败或沉默不退款。"""

    session: SessionRef
    attempt_number: int


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
