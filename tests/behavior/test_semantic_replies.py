"""公开决策：模型代替抽样；叫名兜底、失败沉默与会话隔离。"""
from dataclasses import replace

import pytest

from behavior import create_behavior
from behavior.models import (
    AudioReady,
    NoAction,
    RecognitionObserved,
    ReplyAssessment,
    ReplySettings,
    Speak,
    parse_reply_assessment,
)
from channels.models import RoomTarget, SessionRef
from context.models import ContextRevision


@pytest.mark.parametrize("decision", ["addressed", "contribute", "silent", "invalid", "unavailable"])
@pytest.mark.parametrize("text", ["大肥鱼？", "椭圆方程是啥来着"])
def test_semantic_reply_never_draws_random_and_name_survives_failure(decision, text):
    session = SessionRef("run", "bind", "session", RoomTarget("a", "b"))
    def forbidden_draw():
        raise AssertionError("语义接话不能抽样")
    behavior = create_behavior(random_value=forbidden_draw)
    behavior.start_session(session)
    trigger = AudioReady("input", session, "any-member", ContextRevision())
    permit = behavior.admit(trigger, 0)
    observed = RecognitionObserved(trigger, permit, text, ContextRevision(),
        reply_assessment=ReplyAssessment(session, "input", decision))
    # 100% 也不能把模型沉默/失败变为插话，0% 也不能漏掉叫名/帮助。
    for probability in (0, 100):
        result = behavior.decide(observed, ReplySettings(probability=probability, keywords=("大肥鱼",)))
        assert isinstance(result, Speak if text == "大肥鱼？" or decision in {"addressed", "contribute"} else NoAction)


def test_short_name_is_not_rejected_as_ordinary_short_speech_and_window_cannot_force_reply():
    session = SessionRef("run", "bind", "session", RoomTarget("a", "b"))
    behavior = create_behavior()
    behavior.start_session(session)
    settings = ReplySettings(probability=100, keywords=("大肥鱼",), window_seconds=30, min_utterance_ms=500)
    for ref, text, duration, expected in [("one", "大肥鱼", 300, Speak), ("two", "我继续算我的", 1000, NoAction)]:
        trigger = AudioReady(ref, session, "user", ContextRevision())
        permit = behavior.admit(trigger, 0)
        result = behavior.decide(RecognitionObserved(trigger, permit, text, ContextRevision(), duration,
            reply_assessment=ReplyAssessment(session, ref, "silent")), settings)
        assert isinstance(result, expected)


@pytest.mark.parametrize("raw", [None, {}, {"decision": True}, {"decision": "reply"}, {"decision": "silent", "extra": 1}])
def test_reply_json_rejects_unknown_or_loose_values(raw):
    session = SessionRef("run", "bind", "session", RoomTarget("a", "b"))
    with pytest.raises(ValueError):
        parse_reply_assessment(raw, session, "input")


def test_wrong_input_or_session_cannot_supply_semantic_candidate():
    session = SessionRef("run", "bind", "session", RoomTarget("a", "b"))
    behavior = create_behavior()
    behavior.start_session(session)
    trigger = AudioReady("input", session, "u", ContextRevision())
    permit = behavior.admit(trigger, 0)
    for other, ref in [(replace(session, binding_id="old"), "input"), (session, "other")]:
        with pytest.raises(ValueError):
            RecognitionObserved(trigger, permit, "你好", ContextRevision(),
                reply_assessment=ReplyAssessment(other, ref, "addressed"))
