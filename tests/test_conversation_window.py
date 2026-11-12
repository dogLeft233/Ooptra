
def test_window_is_per_member_extends_expires_and_clears():
    from behavior import create_reply_policy

    now = [100.0]
    window = create_reply_policy(clock=lambda: now[0])
    assert window.observe("ＯＯＰＴＲＡ，好呀", ["ooptra"], "a", 30)  # noqa: RUF001 -- ASR normalization
    assert not window.observe("继续", [], "b", 30)
    now[0] = 129
    assert window.observe("继续", [], "a", 30)
    now[0] = 159
    assert not window.observe("继续", [], "a", 30)
    assert window.observe("ooptra", ["ooptra"], "a", 30)
    window.clear()
    assert not window.observe("继续", [], "a", 30)


def test_unknown_member_never_opens_window_and_disabled_clears():
    from behavior import create_reply_policy

    window = create_reply_policy()
    assert window.observe("ooptra", ["ooptra"], "", 30)
    assert not window.observe("继续", [], "", 30)
    assert window.observe("ooptra", ["ooptra"], "a", 30)
    assert not window.observe("继续", [], "a", 0)
    assert not window.observe("继续", [], "a", 30)


def test_cascade_followups_bypass_zero_probability_until_room_changes():
    from behavior import create_behavior
    from behavior.models import AudioReady, NoAction, RecognitionObserved, ReplySettings, Speak
    from channels.models import RoomTarget, SessionRef
    from context.models import ContextRevision

    behavior = create_behavior()
    current = SessionRef("run", "binding", "one", RoomTarget("a", "c"))
    behavior.start_session(current)
    settings = ReplySettings(probability=0, keywords=("ooptra",), window_seconds=30)
    def say(identifier, text, user):
        trigger = AudioReady(identifier, current, user, ContextRevision())
        permit = behavior.admit(trigger, 0)
        return behavior.decide(RecognitionObserved(trigger, permit, text, ContextRevision()), settings)
    assert isinstance(say("i1", "ooptra，你好", "a"), Speak)
    assert isinstance(say("i2", "然后呢", "a"), Speak)
    assert isinstance(say("i3", "然后呢", "b"), NoAction)
    current = SessionRef("run", "binding", "two", RoomTarget("a", "d"))
    behavior.start_session(current)
    assert isinstance(say("i4", "然后呢", "a"), NoAction)


def test_live_window_requires_unambiguous_input_speaker():
    import time

    from behavior import create_behavior
    from behavior.models import DeferLive, LiveProposal, ReplySettings, Speak
    from channels.models import RoomTarget, SessionRef
    from context.models import ContextRevision

    session = SessionRef("run", "binding", "session", RoomTarget("a", "c"))
    behavior = create_behavior()
    behavior.start_session(session)
    settings = ReplySettings(probability=0, keywords=("ooptra",), window_seconds=30)
    def turn(identifier, text, speaker):
        return behavior.decide_live(LiveProposal(identifier, session, 0, text, speaker, ContextRevision(), time.monotonic()+2), settings)
    assert isinstance(turn("one", "ooptra", "a"), Speak)
    assert isinstance(turn("two", "继续", "b"), DeferLive)
    assert isinstance(turn("three", "继续", "a"), Speak)
    assert isinstance(turn("four", "继续", ""), DeferLive)


def test_live_mixed_speaker_late_keyword_never_opens_member_window():
    import time

    from behavior import create_behavior
    from behavior.models import DeferLive, LiveProposal, ReplySettings, Speak
    from channels.models import RoomTarget, SessionRef
    from context.models import ContextRevision

    session = SessionRef("run", "binding", "session", RoomTarget("a", "c"))
    behavior = create_behavior()
    behavior.start_session(session)
    settings = ReplySettings(probability=0, keywords=("ooptra",), window_seconds=30)
    deadline = time.monotonic()+2
    assert isinstance(behavior.decide_live(LiveProposal("mixed", session, 0, "", "", ContextRevision(), deadline), settings), DeferLive)
    assert isinstance(behavior.decide_live(LiveProposal("mixed", session, 1, "ooptra", "", ContextRevision(), deadline), settings), Speak)
    assert isinstance(behavior.decide_live(LiveProposal("next", session, 0, "继续", "alice", ContextRevision(), deadline), settings), DeferLive)
