"""公开决策准入同时形成输入绑定、有限期限与会话额度事实。"""
from behavior import create_behavior
from behavior.models import AttemptPermit, AttemptRejected, AudioReady
from channels.models import RoomTarget, SessionRef
from context.models import ContextRevision


def session(identifier):
    return SessionRef('run','binding',identifier,RoomTarget('area','room'))


def test_attempts_are_reserved_before_work_and_old_sessions_cannot_spend_new_budget():
    behavior=create_behavior(clock=lambda:10)
    first,second=session('first'),session('second')
    def input(ref,identity):
        return AudioReady(identity,ref,'member',ContextRevision())
    behavior.start_session(first)
    one=input(first,'one')
    assert behavior.admit(one,2)==AttemptPermit(first,1,'one',70)
    assert behavior.admit(one,2)==AttemptPermit(first,1,'one',70)
    # 失败和空识别没有退款入口，开始的尝试已经消耗。
    assert behavior.admit(input(first,'two'),2)==AttemptPermit(first,2,'two',70)
    assert behavior.admit(input(first,'three'),2)==AttemptRejected('limit_reached')
    assert behavior.snapshot().attempts==2
    behavior.start_session(first)
    assert behavior.admit(input(first,'four'),2)==AttemptRejected('limit_reached')
    assert behavior.admit(input(first,'five'),0)==AttemptPermit(first,3,'five',70)
    assert not behavior.exhausted(0)
    behavior.start_session(second)
    assert behavior.admit(input(first,'old'),2)==AttemptRejected('stale_session')
    assert behavior.snapshot().attempts==0
    assert behavior.admit(input(second,'new'),2)==AttemptPermit(second,1,'new',70)
