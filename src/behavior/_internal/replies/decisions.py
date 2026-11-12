"""输入触发去重、意图登记和终态反馈；策略知识集中在行为模块。"""

from collections.abc import Callable
from uuid import uuid4

from behavior._internal.replies.attempts import SessionAttempts
from behavior._internal.replies.policy import MemberReplyPolicy
from behavior.models import (
    AttemptFinished,
    AttemptPermit,
    AttemptRejected,
    AudioReady,
    ControlAction,
    DeferLive,
    FeedbackReceipt,
    NoAction,
    NotExecuted,
    RecognitionObserved,
    ReplySettings,
    Speak,
    SpeechIntent,
)
from channels.models import ActionOutcome, PlaybackResult, SessionRef


class BehaviorService:
    def __init__(self, *, clock: Callable[[], float], random_value: Callable[[], float]) -> None:
        self._clock = clock
        self._policy = MemberReplyPolicy(clock=clock, random_value=random_value)
        self._attempts = SessionAttempts()
        self._triggers: dict[tuple[SessionRef, str], tuple[AudioReady, AttemptPermit | AttemptRejected]] = {}
        self._live = {}
        self._controls = {}
        self._decisions: dict[str, Speak | NoAction | ControlAction] = {}
        self._attempt_results: dict[str, AttemptFinished] = {}
        self._actions: dict[str, NotExecuted | ActionOutcome | PlaybackResult | None] = {}
        self._action_sessions: dict[str, SessionRef] = {}

    def start_session(self, session: SessionRef) -> None:
        if session != self._attempts.snapshot().session:
            self._policy.clear()
            self._triggers.clear()
            self._decisions.clear()
            self._live.clear()
            self._attempt_results.clear()
            self._actions.clear()
            self._action_sessions.clear()
        self._attempts.start_session(session)

    def snapshot(self):
        return self._attempts.snapshot()

    def exhausted(self, limit: int) -> bool:
        return self._attempts.exhausted(limit)

    def admit(self, trigger: AudioReady, limit: int) -> AttemptPermit | AttemptRejected:
        if type(limit) is not int or limit < 0:
            raise ValueError("尝试限额须为非负整数；0表示不限额")
        if trigger.session != self.snapshot().session:
            return AttemptRejected("stale_session")
        key = trigger.session, trigger.trigger_id
        previous = self._triggers.get(key)
        if previous is not None:
            return previous[1] if previous[0] == trigger else AttemptRejected("stale_session")
        # 活动会话有界；容量不足不遗忘有效触发，避免重扣额度。
        if len(self._triggers) >= 1024:
            return AttemptRejected("limit_reached")
        result = self._attempts.admit(trigger.session, limit, input_ref=trigger.trigger_id,
                                      expires_at=self._clock()+60)
        self._triggers[key] = trigger, result
        return result

    def decide(self, observed: RecognitionObserved,
               settings: ReplySettings) -> Speak | NoAction | ControlAction:
        permit, trigger = observed.permit, observed.trigger
        registered = self._triggers.get((trigger.session, trigger.trigger_id))
        if (registered is None or registered[0] != trigger or registered[1] != permit
                or trigger.session != self.snapshot().session):
            return NoAction("stale_attempt")
        previous = self._decisions.get(permit.attempt_id)
        if previous is not None:
            return previous
        if self._clock() >= permit.expires_at:
            return NoAction("expired_attempt")
        control = observed.control
        action_id = uuid4().hex
        if control is not None and ((control.leave and settings.voice_leave_enabled) or
                                    (control.volume is not None and settings.voice_volume_enabled)):
            result = ControlAction(action_id, trigger.session, control)
        elif not observed.text.strip():
            result = NoAction("no_speech")
        elif observed.audio_ms < settings.min_utterance_ms:
            result = NoAction("too_short")
        elif (self._policy.observe(observed.text, list(settings.keywords), trigger.user_key,
                                  settings.window_seconds)
              or self._policy.select(settings.probability)):
            result = Speak(SpeechIntent(uuid4().hex, action_id, trigger.session, trigger.trigger_id,
                                       observed.context_version, min(permit.expires_at, self._clock() + settings.lifetime_seconds),
                                       observed.text, trigger.user_key, permit.attempt_id))
        else:
            result = NoAction("policy_silence")
        self._decisions[permit.attempt_id] = result
        if isinstance(result, (Speak, ControlAction)):
            self._actions[action_id] = None
            self._action_sessions[action_id] = trigger.session
        return result

    def finish_attempt(self, finished: AttemptFinished) -> FeedbackReceipt:
        key = finished.permit.attempt_id
        if finished.permit.session != self.snapshot().session or not any(value[1] == finished.permit for value in self._triggers.values()):
            return FeedbackReceipt("unknown")
        previous = self._attempt_results.get(key)
        if previous is not None:
            return FeedbackReceipt("duplicate" if previous == finished else "conflict")
        self._attempt_results[key] = finished
        return FeedbackReceipt("accepted")

    def feedback(self, result: NotExecuted | ActionOutcome | PlaybackResult) -> FeedbackReceipt:
        if result.action_id not in self._actions:
            return FeedbackReceipt("unknown")
        if isinstance(result, (ActionOutcome, PlaybackResult)) and result.session != self._action_sessions[result.action_id]:
            return FeedbackReceipt("conflict")
        previous = self._actions[result.action_id]
        if previous is not None:
            return FeedbackReceipt("duplicate" if previous == result else "conflict")
        self._actions[result.action_id] = result
        if isinstance(result, NotExecuted) and result.stage == "permission":
            for key, (_, decision, selected) in list(self._live.items()):
                if isinstance(decision, Speak) and decision.intent.action_id == result.action_id:
                    self._live[key] = (-1, DeferLive(self._clock()), selected)
        return FeedbackReceipt("accepted")

    def decide_live(self, proposal, settings):
        if proposal.session != self.snapshot().session:
            return NoAction("stale_session")
        key = "live:" + proposal.proposal_id
        previous = self._live.get(key)
        if previous is not None:
            old_revision, result, selected = previous
            if isinstance(result, (Speak, NoAction)) or proposal.revision <= old_revision:
                return result
        else:
            if self._clock() >= proposal.deadline:
                return NoAction("expired_proposal")
            selected = self._policy.select(settings.probability)
        if self._clock() >= proposal.deadline:
            return NoAction("expired_proposal")
        if len(self._live) >= 1024 and key not in self._live:
            return NoAction("capacity")
        named = self._policy.observe(proposal.text, list(settings.keywords), proposal.user_key,
                                     settings.window_seconds)
        if selected or named or proposal.internal:
            action_id = uuid4().hex
            result = Speak(SpeechIntent(uuid4().hex, action_id, proposal.session, proposal.proposal_id,
                                        proposal.context_version, self._clock() + settings.lifetime_seconds,
                                        proposal.text, proposal.user_key))
            self._actions[action_id] = None
            self._action_sessions[action_id] = proposal.session
        elif self._clock() < proposal.deadline and settings.keywords:
            result = DeferLive(proposal.deadline)
        else:
            result = NoAction("policy_silence")
        self._live[key] = proposal.revision, result, selected
        return result


    def decide_control(self, proposal, settings):
        previous = self._controls.get(proposal.trigger_id)
        if previous is not None:
            return previous[1] if previous[0] == proposal else NoAction("control_conflict")
        candidate = proposal.candidate
        if proposal.session != self.snapshot().session:
            return NoAction("stale_session")
        if len(self._controls) >= 1024:
            return NoAction("capacity")
        allowed = ((candidate.leave and settings.voice_leave_enabled)
                   or (candidate.volume is not None and settings.voice_volume_enabled))
        if allowed:
            result = ControlAction(uuid4().hex, proposal.session, candidate)
            self._actions[result.action_id] = None
            self._action_sessions[result.action_id] = proposal.session
        else:
            result = NoAction("control_disabled")
        self._controls[proposal.trigger_id] = proposal, result
        return result
