from channels.models import RoomTarget, SessionRef
from context.models import ContextRevision


def test_same_trigger_spends_one_attempt_and_duplicate_feedback_is_visible():
    from behavior import create_behavior
    from behavior.models import (
        AttemptFinished,
        AudioReady,
        RecognitionObserved,
        ReplySettings,
        Speak,
    )

    session = SessionRef("run", "bind", "session", RoomTarget("a", "b"))
    behavior = create_behavior()
    behavior.start_session(session)
    trigger = AudioReady("input", session, "user", ContextRevision(1))
    permit = behavior.admit(trigger, 1)
    assert behavior.admit(trigger, 1) == permit
    assert behavior.snapshot().attempts == 1
    decision = behavior.decide(RecognitionObserved(trigger, permit, "你好", ContextRevision(1)), ReplySettings(probability=100))
    assert isinstance(decision, Speak)
    assert behavior.decide(RecognitionObserved(trigger, permit, "你好", ContextRevision(1)), ReplySettings(probability=100)) == decision
    assert behavior.finish_attempt(AttemptFinished(permit, "completed", 12)).status == "accepted"
    assert behavior.finish_attempt(AttemptFinished(permit, "completed", 12)).status == "duplicate"
    assert behavior.finish_attempt(AttemptFinished(permit, "failed", 12)).status == "conflict"


def test_changed_trigger_cannot_reuse_an_existing_permit_to_obtain_an_intent():
    from dataclasses import replace

    from behavior import create_behavior
    from behavior.models import AudioReady, NoAction, RecognitionObserved, ReplySettings, Speak
    session = SessionRef("run", "bind", "session", RoomTarget("a", "b"))
    behavior = create_behavior()
    behavior.start_session(session)
    trigger = AudioReady("input", session, "alice", ContextRevision())
    permit = behavior.admit(trigger, 0)
    assert isinstance(behavior.decide(RecognitionObserved(trigger, permit, "hello", ContextRevision()), ReplySettings()), Speak)
    changed = replace(trigger, user_key="bob")
    assert isinstance(behavior.decide(RecognitionObserved(changed, permit, "hello", ContextRevision()), ReplySettings()), NoAction)


def test_expired_attempt_cannot_start_expression_but_does_not_refund_quota():
    from behavior import create_behavior
    from behavior.models import AudioReady, NoAction, RecognitionObserved, ReplySettings
    now = [1.0]
    session = SessionRef("run", "bind", "session", RoomTarget("a", "b"))
    behavior = create_behavior(clock=lambda: now[0])
    behavior.start_session(session)
    trigger = AudioReady("input", session, "u", ContextRevision())
    permit = behavior.admit(trigger, 1)
    now[0] = 62.0
    assert isinstance(behavior.decide(RecognitionObserved(trigger, permit, "hello", ContextRevision()), ReplySettings()), NoAction)
    assert behavior.snapshot().attempts == 1


def test_old_session_live_proposal_never_reuses_current_action_or_consumes_randomness():
    from behavior import create_behavior
    from behavior.models import LiveProposal, NoAction, ReplySettings, Speak
    current = SessionRef("run", "bind", "session", RoomTarget("a", "b"))
    old = SessionRef("old-run", "old-bind", "session", RoomTarget("a", "b"))
    draws = []
    behavior = create_behavior(random_value=lambda: draws.append(1) or .1)
    behavior.start_session(current)
    settings = ReplySettings(probability=30)
    assert isinstance(behavior.decide_live(LiveProposal("proposal", current, 0, "hello", "u", ContextRevision(), 10**12), settings), Speak)
    result = behavior.decide_live(LiveProposal("proposal", old, 0, "hello", "u", ContextRevision(), 10**12), settings)
    assert isinstance(result, NoAction)
    assert draws == [1]


def test_permission_retry_keeps_one_probability_draw_for_the_original_turn():
    from behavior import create_behavior
    from behavior.models import LiveProposal, NotExecuted, ReplySettings, Speak
    draws = iter([.1, .9])
    behavior = create_behavior(random_value=lambda: next(draws))
    session = SessionRef("run", "bind", "session", RoomTarget("a", "b"))
    behavior.start_session(session)
    proposal = LiveProposal("proposal", session, 0, "hello", "u", ContextRevision(), 10**12)
    settings = ReplySettings(probability=30)
    first = behavior.decide_live(proposal, settings)
    assert isinstance(first, Speak)
    behavior.feedback(NotExecuted(first.intent.action_id, "permission", "proposal_revision"))
    retry = behavior.decide_live(proposal, settings)
    assert isinstance(retry, Speak)
    assert retry.intent.action_id != first.intent.action_id


def test_attempt_identity_includes_full_session_and_old_feedback_is_unknown():
    from behavior import create_behavior
    from behavior.models import AttemptFinished, AudioReady
    old = SessionRef("old-run", "old-bind", "same-id", RoomTarget("a", "b"))
    new = SessionRef("new-run", "new-bind", "same-id", RoomTarget("a", "b"))
    behavior = create_behavior()
    behavior.start_session(old)
    previous = behavior.admit(AudioReady("old-input", old, "u", ContextRevision()), 1)
    behavior.start_session(new)
    current = behavior.admit(AudioReady("new-input", new, "u", ContextRevision()), 1)
    assert previous.attempt_id != current.attempt_id
    assert behavior.finish_attempt(AttemptFinished(current, "completed", 1)).status == "accepted"
    assert behavior.finish_attempt(AttemptFinished(previous, "completed", 1)).status == "unknown"
    assert behavior.snapshot().session == new and behavior.snapshot().attempts == 1


def test_wrong_session_execution_feedback_cannot_consume_current_action():
    from behavior import create_behavior
    from behavior.models import AudioReady, RecognitionObserved, ReplySettings
    from channels.models import ActionOutcome
    current = SessionRef("run", "bind", "session", RoomTarget("a", "b"))
    old = SessionRef("old", "bind", "session", RoomTarget("a", "b"))
    behavior = create_behavior()
    behavior.start_session(current)
    trigger = AudioReady("input", current, "u", ContextRevision())
    permit = behavior.admit(trigger, 0)
    decision = behavior.decide(RecognitionObserved(trigger, permit, "hello", ContextRevision()), ReplySettings())
    action_id = decision.intent.action_id
    assert behavior.feedback(ActionOutcome(action_id, "completed", old)).status == "conflict"
    assert behavior.feedback(ActionOutcome(action_id, "completed", current)).status == "accepted"
