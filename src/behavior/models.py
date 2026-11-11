"""行为模块的不可变结果；不包含传输或模型对象。"""

from dataclasses import dataclass
from typing import Literal

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
