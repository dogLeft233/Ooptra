"""上下文持有的供应商无关数据。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from channels.models import ActionOutcome, PlaybackResult, RoomTarget, SessionRef


@dataclass(frozen=True)
class MemoryTurn:
    """本地 JSONL 历史条目；ts 为秒级时间戳。"""

    role: str
    content: str
    user_key: str = ""
    channel_key: str = ""
    ts: float = 0.0

    @classmethod
    def parse(cls, value: Any) -> MemoryTurn | None:
        """将旧 JSONL 行转换为已知字段；非对象行不属于历史条目。"""
        if not isinstance(value, dict):
            return None
        role = str(value.get("role") or "user")
        if role not in {"user", "assistant", "system"}:
            role = "user"
        timestamp = value.get("ts", 0.0)
        return cls(
            role=role,
            content=str(value.get("content") or ""),
            user_key=str(value.get("user_key") or ""),
            channel_key=str(value.get("channel_key") or ""),
            ts=float(timestamp) if type(timestamp) in {int, float} else 0.0,
        )


@dataclass(frozen=True)
class ConfirmedTurn:
    """应用提交的输入事实及实际输出；生成草稿不能作为已输出回复。

    completed 仅在调用方已确认音频完成/文字发送后使用。
    其他状态只提交用户输入，reply_text 必须为空。
    不提供跨提交幂等或跨 AstrBot 投递事务保证。
    """

    user_text: str
    reply_text: str
    confirmation: Literal["completed", "silent", "interrupted", "failed", "unknown"]
    user_key: str = ""
    channel_key: str = ""

    def __post_init__(self) -> None:
        if self.confirmation not in {"completed", "silent", "interrupted", "failed", "unknown"}:
            raise ValueError("未知输出确认状态")
        if not all(isinstance(value, str) for value in
                   (self.user_text, self.reply_text, self.user_key, self.channel_key)):
            raise ValueError("输入与确认输出必须为文字")
        if self.reply_text and self.confirmation != "completed":
            raise ValueError("未确认完成的输出不能写为完整助手回复")
        if self.confirmation == "completed" and not self.reply_text.strip():
            raise ValueError("确认完成的输出必须含有回复文字")


@dataclass(frozen=True)
class RecordReceipt:
    """仅确认本地文件写入返回成功；不表示外部记忆收到或持久化 fsync。"""

    input_recorded: bool
    output_recorded: bool
    delivery_id: str | None = None
    delivery_admission: str = "not_required"


@dataclass(frozen=True)
class ContextRevision:
    observation_version: int = 0
    source_generation: int = 0
    history_revision: int = 0


@dataclass(frozen=True)
class ContextRef:
    session: SessionRef
    source_generation: int
    binding_id: str


@dataclass(frozen=True)
class HistoryMessage:
    role: str
    content: str


@dataclass(frozen=True)
class Observation:
    event_id: str
    session: SessionRef
    connection_id: str
    sequence: int
    kind: Literal["session", "recognized", "members", "activity"]
    text: str = ""
    user_key: str = ""
    input_ref: str = ""
    members: tuple[str, ...] = ()
    complete: bool = True
    context_ref: ContextRef | None = None

    def __post_init__(self) -> None:
        if not self.event_id or not self.connection_id or type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("观察需要事件、连接和非负序号")
        if self.kind not in {"session", "recognized", "members", "activity"}:
            raise ValueError("未知观察类型")
        if self.context_ref is not None and not isinstance(self.context_ref, ContextRef):
            raise ValueError("观察来源必须是ContextRef")
        if self.context_ref is not None and self.context_ref.session != self.session:
            raise ValueError("观察来源与渠道会话不一致")
        if self.kind == "recognized" and not self.input_ref:
            raise ValueError("识别观察需要稳定输入引用")


@dataclass(frozen=True)
class SituationSnapshot:
    scope: RoomTarget
    session: SessionRef | None
    revision: ContextRevision
    members: tuple[str, ...] = ()
    complete: bool = False
    latest_input: str = ""
    history: tuple[HistoryMessage, ...] = ()


@dataclass(frozen=True)
class ContextRequest:
    session: SessionRef
    user_key: str = ""
    input_ref: str = ""
    persona: str = ""
    known_revision: ContextRevision | None = None


@dataclass(frozen=True)
class WriteAuthorization:
    grant_id: str
    context_ref: ContextRef
    input_ref: str
    expires_at: float


@dataclass(frozen=True)
class PreparedContext:
    context_ref: ContextRef
    revision: ContextRevision
    persona: str
    history: tuple[HistoryMessage, ...]
    authorized_write_grants: tuple[WriteAuthorization, ...] = ()
    source_state: Literal["ready", "local", "unavailable"] = "local"

    @property
    def version(self) -> ContextRevision:
        return self.revision


@dataclass(frozen=True)
class OutputRecord:
    action_id: str
    session: SessionRef
    turn: ConfirmedTurn
    outcome: ActionOutcome | PlaybackResult
    context_ref: ContextRef | None = None
    input_ref: str = ""

    def __post_init__(self) -> None:
        if not self.action_id or self.action_id != self.outcome.action_id:
            raise ValueError("输出记录必须关联实际动作")
        if self.outcome.session is not None and self.outcome.session != self.session:
            raise ValueError("输出结果不属于原会话")
        if self.turn.confirmation == "completed" and self.outcome.status != "completed":
            raise ValueError("未完成执行不能记录完整回复")
        if self.context_ref is not None and self.context_ref.session != self.session:
            raise ValueError("来源不属于原会话")


@dataclass(frozen=True)
class DeliveryStatus:
    delivery_id: str
    state: Literal["pending", "retrying", "confirmed", "failed", "unknown"]
    target_receipts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RecallRequest:
    tool_call_id: str
    context_ref: ContextRef
    query: str
    speaker: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tool_call_id, str) or not self.tool_call_id or not isinstance(self.query, str) or not 1 <= len(self.query.strip()) <= 2000:
            raise ValueError("记忆查询需要调用 ID 和 1-2000 字内容")


@dataclass(frozen=True)
class RememberRequest:
    tool_call_id: str
    context_ref: ContextRef
    content: str
    authorization: WriteAuthorization
    speaker: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tool_call_id, str) or not self.tool_call_id or not isinstance(self.content, str) or not 1 <= len(self.content.strip()) <= 2000 or not isinstance(self.authorization, WriteAuthorization):
            raise ValueError("记忆写入需要调用 ID、授权和 1-2000 字内容")


@dataclass(frozen=True)
class LegacyExport:
    directory: str
    history_count: int
    delivery_count: int
    manifest: str


@dataclass(frozen=True)
class PollRequest:
    client_id: str
    wait: float = 20.0

    def __post_init__(self) -> None:
        import math
        if not isinstance(self.client_id, str) or not self.client_id or type(self.wait) not in {int, float} or not math.isfinite(self.wait) or not 0 <= self.wait <= 20:
            raise ValueError("轮询需要实例 ID 和 0-20 秒等待")


@dataclass(frozen=True)
class PollResult:
    status: Literal["events", "timeout", "peer_conflict", "closed"]
    events_json: tuple[str, ...] = ()

    def events(self) -> list[dict[str, Any]]:
        import json
        return [json.loads(event) for event in self.events_json]


@dataclass(frozen=True)
class RpcReceipt:
    request_id: str
    client_id: str
    result_json: str

    @classmethod
    def parse(cls, value: Any) -> RpcReceipt:
        import json
        if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k] for k in ("request_id", "client_id")) or not isinstance(value.get("result"), dict):
            raise ValueError("回执需要请求 ID、实例 ID 和结果对象")
        result = value["result"]
        if type(result.get("ok")) is not bool or ("retry" in result and type(result["retry"]) is not bool):
            raise ValueError("回执结果需要明确成功状态")
        return cls(value["request_id"], value["client_id"], json.dumps(result, ensure_ascii=False))


@dataclass(frozen=True)
class RpcResolution:
    status: Literal["accepted", "duplicate", "unknown", "rejected"]

    @property
    def accepted(self) -> bool:
        return self.status in {"accepted", "duplicate"}


@dataclass(frozen=True)
class BindingRequest:
    action: Literal["list", "save", "delete", "source-save", "source-delete", "source-options"]
    payload_json: str

    @classmethod
    def parse(cls, value: Any) -> BindingRequest:
        import json
        if not isinstance(value, dict) or value.get("action") not in {"list", "save", "delete", "source-save", "source-delete", "source-options"}:
            raise ValueError("不支持的来源管理操作")
        return cls(value["action"], json.dumps(value, ensure_ascii=False))
