"""行为策略的已实现公开边界；完整决策/反馈契约仍在迁移。"""

from typing import Protocol

from behavior.models import AttemptPermit, AttemptRejected, AttemptsSnapshot
from channels.models import SessionRef


class CascadeAttempts(Protocol):
    """在同一事件循环内、首次模型等待前原子预留会话输入额度。

    每次 admit 代表一次新输入；调用者不得对同一输入重复调用。0 表示不限额。
    仅实际新会话重置；旧会话输入拒绝，不改变新会话额度。没有失败退款。
    """

    def start_session(self, session: SessionRef) -> None: ...

    def admit(self, session: SessionRef, limit: int) -> AttemptPermit | AttemptRejected: ...

    def exhausted(self, limit: int) -> bool: ...

    def snapshot(self) -> AttemptsSnapshot: ...


class ReplyPolicy(Protocol):
    """概率、关键词规范化和有界成员窗口；调用者不维护窗口字典。

    observe 对已知成员延长关键词/连续对话窗口；未知成员不能获得窗口。
    clear 在换房/混合成员无法归属时清理；select 每次调用独立抽样，
    同一回合的最终选择及 Live 的迟到信息协调仍由旧后端持有。
    """

    def select(self, percent: int) -> bool: ...

    def matches_keywords(self, text: str, keywords: list[str]) -> bool: ...

    def observe(self, text: str, keywords: list[str], user_key: str, seconds: float) -> bool: ...

    def clear(self) -> None: ...
