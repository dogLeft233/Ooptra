"""上下文持有的供应商无关数据。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


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
