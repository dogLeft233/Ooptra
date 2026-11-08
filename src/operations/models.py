"""系统维护公开值类型、版本化配置与计划，以及外部平台端口。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

DEPLOYMENT_CONTRACT_VERSION = 2
CONTEXT_SCHEMA_MAX = 1


@dataclass(frozen=True)
class OutputVolume:
    """已确认可保存的输出音量百分比；临时静音不是持久化配置。"""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int or not 10 <= self.value <= 100:
            raise ValueError("voice.output_volume 需要 10 到 100 之间的整数")

    @classmethod
    def parse(cls, raw: object) -> OutputVolume:
        """保留配置编辑器对十进制整数字符串的兼容，布尔和小数均拒绝。"""
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("voice.output_volume 需要整数") from exc
        return cls(value)


@dataclass(frozen=True)
class VolumeSaveReceipt:
    """磁盘与运行配置字典已同步；不声称播放增益已经热应用。"""

    volume: int


_PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


def validate_network_field(field, value):
    """Validate before saving, without including a secret input in the error."""
    if field not in {"update_proxy", "update_mirror"}:
        raise ValueError("未知更新网络配置项")
    hint = "更新代理需填写完整代理 URL、direct 或留空" if field == "update_proxy" else "更新镜像需填写完整 HTTPS Git 仓库 URL 或留空"
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError(hint)
    if any(ord(char) < 32 or ord(char) == 127 for char in unquote(value)):
        raise ValueError(hint)
    value = value.strip()
    if not value:
        return ""
    if field == "update_proxy" and value.lower() == "direct":
        return "direct"
    try:
        parsed = urlsplit(value)
        if not parsed.hostname or (parsed.port is not None and not 1 <= parsed.port <= 65535):
            raise ValueError
        if any(char.isspace() for char in value) or any(char in value for char in "\\?#"):
            raise ValueError
        if field == "update_proxy":
            if parsed.scheme not in _PROXY_SCHEMES or parsed.path not in {"", "/"}:
                raise ValueError
        elif (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
              or not parsed.path.strip("/") or any(char in value for char in "{}")):
            raise ValueError
    except ValueError:
        raise ValueError(hint) from None
    return value


@dataclass(frozen=True)
class NetworkSettings:
    proxy: str = field(default="", repr=False)
    mirror: str = ""

    def __post_init__(self):
        object.__setattr__(self, "proxy", validate_network_field("update_proxy", self.proxy))
        object.__setattr__(self, "mirror", validate_network_field("update_mirror", self.mirror))




class MaintenanceClock(Protocol):
    def monotonic(self) -> float: ...
    def time(self) -> float: ...


@dataclass
class MaintenancePlatform:
    """可注入的平台端口。回调只处理 OS/网络/存储和时钟，不拥有维护状态。"""

    run_command: Callable[..., Any]
    inventory: Callable[..., Any]
    delete_items: Callable[..., Any]
    lookup_official_sha: Callable[..., Any]
    fetch_verified: Callable[..., Any]
    runtime_health: Callable[..., Any]
    clock: MaintenanceClock
    version: str
    test_mirror_connection: Callable[..., Any]
    query_wait_seconds: float = 12
    preflight_seconds: float = 10
    installed_seconds: float = 4
    check_seconds: float = 60


@dataclass(frozen=True)
class UpdateTarget:
    """已确认的渠道和完整提交；后续准备过程直接使用此结构。"""

    channel: str
    commit: str

    def __post_init__(self):
        import re
        if (not isinstance(self.channel, str) or self.channel not in {"main", "beta", "dev"}
                or not isinstance(self.commit, str)
                or re.fullmatch(r"[a-f0-9]{40}", self.commit) is None):
            raise ValueError("更新参数无效")

    @classmethod
    def parse(cls, channel: object, commit: object):
        if not isinstance(channel, str) or not isinstance(commit, str):
            raise ValueError("更新参数无效")
        return cls(channel, commit)


def _freeze_configuration_value(value):
    """冻结已归一化的配置值，避免校验后被调用方改写。"""
    from types import MappingProxyType
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_configuration_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_configuration_value(item) for item in value)
    if isinstance(value, float):
        import math
        if not math.isfinite(value):
            raise ValueError("配置值不能包含非有限数字")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError("配置更新包含不支持的值")


def _thaw_configuration_value(value):
    from collections.abc import Mapping
    if isinstance(value, Mapping):
        return {key: _thaw_configuration_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_configuration_value(item) for item in value]
    return value


@dataclass(frozen=True)
class ConfigurationGroup:
    """一个已解析的配置字典及字段路径；不携带界面 schema。"""

    name: str
    source: str
    fields: tuple[tuple[str, Any], ...] = field(repr=False)

    def __post_init__(self):
        import keyword
        if (not isinstance(self.name, str) or not self.name
                or not isinstance(self.source, str) or not self.source.isidentifier()
                or keyword.iskeyword(self.source) or not self.fields):
            raise ValueError("配置分组或来源无效")
        names = set()
        for name, _ in self.fields:
            if not isinstance(name, str) or not name or any(not part for part in name.split(".")) or name in names:
                raise ValueError("配置字段路径无效或重复")
            names.add(name)
        object.__setattr__(self, "fields", tuple(
            (name, _freeze_configuration_value(value)) for name, value in self.fields))


@dataclass(frozen=True)
class ConfigurationChange:
    """输入边界已批准的字段更新；拥有快照，后续不再依赖原始输入。

    构造只确认结构及值可保存，不授予字段编辑权限；HTTP 白名单仍由输入边界解析。
    值不出现在 repr/回执中，to_updates 每次返回独立的可写副本。
    """

    groups: tuple[ConfigurationGroup, ...]

    def __post_init__(self):
        object.__setattr__(self, "groups", tuple(self.groups))
        if not self.groups or len({group.name for group in self.groups}) != len(self.groups):
            raise ValueError("没有需要保存的改动或配置分组重复")
        if len({group.source for group in self.groups}) != len(self.groups):
            raise ValueError("多个配置分组不能写同一个来源")

    @classmethod
    def from_normalized(cls, updates: dict[str, dict[str, Any]], *, sources: dict[str, str]):
        if not isinstance(updates, dict) or any(
            name not in sources or not isinstance(values, dict) for name, values in updates.items()
        ):
            raise ValueError("配置更新或来源无效")
        return cls(tuple(ConfigurationGroup(name, sources[name], tuple(values.items()))
                         for name, values in updates.items()))

    def to_updates(self) -> dict[str, dict[str, Any]]:
        return {group.name: {name: _thaw_configuration_value(value) for name, value in group.fields}
                for group in self.groups}

    def source_names(self) -> dict[str, str]:
        return {group.name: group.source for group in self.groups}


@dataclass(frozen=True)
class ConfigurationSaveReceipt:
    """配置磁盘与所保存运行字段已同步；不表示业务组件已热应用。"""

    changed: tuple[tuple[str, tuple[str, ...]], ...]

    def changed_fields(self) -> dict[str, list[str]]:
        return {group: list(fields) for group, fields in self.changed}


class ReasoningEffort(str, Enum):
    """DeepSeek 已确认的思考档位；none 显式关闭思考。"""

    NONE = "none"
    LOW = "low"
    HIGH = "high"
    MAX = "max"


@dataclass
class VoiceAgentSettings:
    enabled: bool = False
    backend: str = "gemini_live"
    persona: str = "你是 Oopz 语音频道助手，说话简洁友好，像在语音房里自然聊天。"
    area: str = ""
    channel: str = ""
    barge_in: bool = True
    #: 抢话门限：房间里某人要**连续说话**满这么多毫秒，才认作「真的要打断 bot」。
    #: 0 = 关闭门限（退回旧行为：第一帧就打断，热闹房间里 bot 说不完一句话）。
    barge_in_hold_ms: int = 300
    vad_mode: str = "local"
    listen_only_uids: list[str] = field(default_factory=list)
    reply_text_to_channel: bool = False
    # 自动接话概率；快捷开口与进退房台词始终可用。
    reply_probability_percent: int = 30
    force_reply_keywords: list[str] = field(default_factory=list)
    conversation_window_enabled: bool = True
    conversation_window_seconds: int = 30
    voice_leave_enabled: bool = True
    voice_volume_enabled: bool = True
    sample_rate_in: int = 16000
    sample_rate_out: int = 24000
    output_volume: int = 50
    #: 说完多久算一句。700ms 对中文太短 —— 想词、换气的自然停顿就会被当成
    #: 「说完了」，一句话被切成几段，bot 逐段回（表现为「我每句话必回」）。
    silence_ms: int = 1200
    max_utterance_ms: int = 15000
    #: 短于这么多毫秒的音频不送 ASR。实测「对。」0.1s、「哦。」0.4s 这类一个字
    #: 的气声，回了也只是噪音。``_MIN_UTTERANCE_RMS`` 按**能量**过滤，管不到它们。
    min_utterance_ms: int = 500
    #: 回合节流：一条回合**完整说完**之后，至少隔这么多毫秒才允许开始下一回合。
    #: 不节流的话 bot 一直在出声，用户说下一句时它还没说完 —— 队列只增不减，
    #: 体感就是「延迟越来越高」。被人抢话打断的回合不计冷却（人家要的是立刻回应）。
    #: 0 = 不冷却。
    reply_cooldown_ms: int = 2500
    # 级联首段等待与播出后的断流等待；仍受原行动绝对截止约束。
    first_audio_timeout_seconds: int = 45
    audio_gap_timeout_seconds: int = 10
    # 每次进房的级联自动接话尝试上限；0 不限制，失败也消耗额度。
    cascade_turn_limit: int = 0

    # 模型 API 网络出口：""=直连/系统，"clash"=127.0.0.1:7890，或显式 URL
    proxy: str = field(default="", repr=False)

    # MiMo 级联
    mimo_api_key: str = field(default="", repr=False)
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_asr_model: str = "mimo-v2.5-asr"
    mimo_llm_model: str = "mimo-v2.6-flash"
    mimo_tts_model: str = "mimo-v2.5-tts"
    mimo_tts_voice: str = "冰糖"

    # Aliyun ASR/TTS + DeepSeek. Keys never enter the WebUI configuration schema.
    dashscope_api_key: str = field(default="", repr=False)
    dashscope_ws_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    aliyun_asr_model: str = "qwen-audio-3.1-asr-flash-message"
    aliyun_tts_model: str = "qwen-audio-3.1-tts-flash"
    aliyun_tts_voice: str = "longanhuan_v3.1"
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_reasoning_effort: ReasoningEffort = ReasoningEffort.LOW

    # Gemini Live
    gemini_api_key: str = field(default="", repr=False)
    gemini_base_url: str = (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    )
    gemini_model: str = "gemini-2.0-flash-live-001"
    gemini_voice: str = "Puck"

    # OpenAI Realtime（骨架）
    openai_api_key: str = field(default="", repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_realtime_model: str = "gpt-4o-mini-realtime-preview"
    openai_voice: str = "alloy"

    memory_path: str = "data/voice_memory.jsonl"
    memory_max_turns: int = 30

    def __post_init__(self) -> None:
        for value, maximum in ((self.first_audio_timeout_seconds, 120), (self.audio_gap_timeout_seconds, 60)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("级联音频等待必须为范围内的整数秒数")


@dataclass
class VoiceApiSettings:
    # 独立端口默认关闭：语音接口已合并到 WebUI /api/*
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 3091
    token: str = field(default="", repr=False)


class RevisionConflict(ValueError):
    """调用者所见配置已过期，必须重新读取，不能覆盖新配置。"""


@dataclass(frozen=True)
class ConfigurationRequest:
    request_id: str
    expected_revision: str
    change: ConfigurationChange

    def __post_init__(self):
        import re
        if (not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 128
                or re.fullmatch(r'[a-zA-Z0-9_.:-]+', self.request_id) is None
                or not isinstance(self.expected_revision, str)
                or re.fullmatch(r'[a-f0-9]{64}', self.expected_revision) is None
                or not isinstance(self.change, ConfigurationChange)):
            raise ValueError('配置请求标识或版本无效')


@dataclass(frozen=True)
class ConfigurationResult:
    request_id: str
    revision: str
    state: str
    changed: tuple[tuple[str, tuple[str, ...]], ...] = ()
    applied: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    restart_required: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeApplyReport:
    applied: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    restart_required: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuiesceResult:
    closed: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.pending and not self.errors


@dataclass(frozen=True)
class HandoffRef:
    job_id: str
    revision: int
    token: str = field(repr=False)


@dataclass(frozen=True)
class ExitResult:
    accepted: bool
    reason: str = ''


@dataclass(frozen=True)
class RuntimeHealth:
    ready: bool
    degraded: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeploymentAccess:
    allowed: bool
    reason: str = ''
    job_id: str = ''


@dataclass(frozen=True)
class MaintenanceRequest:
    action: str
    target: UpdateTarget | None = None
    backup_id: str = ''
    confirm_channel_switch: bool = False

    def __post_init__(self):
        import re
        if self.action == 'update':
            if not isinstance(self.target, UpdateTarget) or self.backup_id:
                raise ValueError('更新计划需要已解析的更新目标')
        elif self.action == 'restore':
            if self.target is not None or re.fullmatch(r'[a-f0-9]{32}', self.backup_id) is None:
                raise ValueError('恢复计划需要有效备份引用')
        else:
            raise ValueError('未知维护动作')


@dataclass(frozen=True)
class MaintenancePlan:
    plan_id: str
    request: MaintenanceRequest
    deployment_revision: str
    config_revision: str
    expires_at: float
    impact: tuple[str, ...]
    backup_fingerprint: str = ""


@dataclass(frozen=True)
class JobRef:
    job_id: str


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    revision: int
    action: str
    phase: str
    detail: str
    closed: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()


@dataclass(frozen=True)
class SystemSnapshot:
    access: DeploymentAccess
    job: JobSnapshot | None


@dataclass(frozen=True)
class StoppedDeployment:
    job_id: str
    pid: int
    start_marker: str
    handoff_token: str = field(repr=False)


@dataclass(frozen=True)
class ValidatedRestore:
    backup_id: str
    fingerprint: str



class CredentialsError(ValueError):
    """本地凭据无法解析或不能安全持久化，不携带凭据正文。"""
