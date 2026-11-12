"""未解析标量不能变成行为许可、强制发言或无限窗口。"""
import time

import pytest

from behavior import create_behavior
from behavior.models import (
    AttemptPermit,
    AudioReady,
    LiveProposal,
    RecognitionObserved,
    ReplySettings,
    SpeechIntent,
)
from channels.models import RoomTarget, SessionRef
from context.models import ContextRevision


def session():
    return SessionRef('run','binding','room',RoomTarget('a','b'))


@pytest.mark.parametrize('change',[
    {'voice_leave_enabled':'false'}, {'voice_volume_enabled':1},
    {'window_seconds':float('nan')}, {'window_seconds':float('inf')},
    {'window_seconds':-1}, {'window_seconds':True},
])
def test_unparsed_controls_and_unbounded_windows_are_rejected(change):
    with pytest.raises(ValueError):
        ReplySettings(**change)


@pytest.mark.parametrize('change',[
    {'internal':'false'}, {'revision':True}, {'revision':-1},
    {'proposal_id':True}, {'deadline':float('nan')},
])
def test_live_proposal_cannot_forge_internal_permission_or_revision(change):
    values=dict(proposal_id='proposal',session=session(),revision=0,text='',user_key='',
                context_version=ContextRevision(),deadline=time.monotonic()+5)
    values.update(change)
    with pytest.raises(ValueError):
        LiveProposal(**values)


@pytest.mark.parametrize('change',[
    {'attempt_number':True}, {'policy_version':True}, {'input_ref':42},
    {'expires_at':float('nan')}, {'expires_at':float('inf')},
])
def test_attempt_permit_must_be_bound_and_have_finite_expiry(change):
    values=dict(session=session(),attempt_number=1,input_ref='input',policy_version=1,
                expires_at=time.monotonic()+5)
    values.update(change)
    with pytest.raises(ValueError):
        AttemptPermit(**values)


def test_nan_duration_cannot_bypass_minimum_utterance_policy():
    behavior=create_behavior()
    ref=session()
    behavior.start_session(ref)
    trigger=AudioReady('input',ref,'u',ContextRevision())
    permit=behavior.admit(trigger,1)
    with pytest.raises(ValueError):
        RecognitionObserved(trigger,permit,'hello',ContextRevision(),audio_ms=float('nan'))


@pytest.mark.parametrize('change',[{'action_id':True},{'trigger_id':42},{'announcement_kind':'unknown'}])
def test_expression_identity_and_announcement_branch_are_parsed_values(change):
    values=dict(intent_id='intent',action_id='action',session=session(),trigger_id='input',
                context_version=ContextRevision(),deadline=time.monotonic()+5)
    values.update(change)
    with pytest.raises(ValueError):
        SpeechIntent(**values)


@pytest.mark.parametrize('limit',[True,-1,float('nan')])
def test_invalid_limit_cannot_turn_into_unlimited_attempts(limit):
    behavior=create_behavior()
    ref=session()
    behavior.start_session(ref)
    with pytest.raises(ValueError):
        behavior.admit(AudioReady('input',ref,'u',ContextRevision()),limit)
    assert behavior.snapshot().attempts==0
