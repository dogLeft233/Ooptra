"""通过行为公开入口验证会话尝试额度，不启动渠道或模型。"""

from behavior import create_cascade_attempts
from behavior.models import AttemptPermit, AttemptRejected
from channels.models import RoomTarget, SessionRef


def session(identifier):
    return SessionRef("run", "binding", identifier, RoomTarget("area", "room"))


def test_attempts_are_reserved_before_work_and_old_sessions_cannot_spend_new_budget():
    budget = create_cascade_attempts()
    first, second = session("first"), session("second")
    budget.start_session(first)
    assert budget.admit(first, 2) == AttemptPermit(first, 1)
    # 失败和空识别没有退款入口：开始的尝试已经消耗。
    assert budget.admit(first, 2) == AttemptPermit(first, 2)
    assert budget.admit(first, 2) == AttemptRejected("limit_reached")
    assert budget.snapshot().attempts == 2
    budget.start_session(first)
    assert budget.admit(first, 2) == AttemptRejected("limit_reached")
    assert budget.admit(first, 0) == AttemptPermit(first, 3)
    assert not budget.exhausted(0)
    budget.start_session(second)
    assert budget.admit(first, 2) == AttemptRejected("stale_session")
    assert budget.snapshot().attempts == 0
    assert budget.admit(second, 2) == AttemptPermit(second, 1)
