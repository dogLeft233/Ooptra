"""级联流程只传递公开事实；行为拥有选择，生成拥有流，渠道拥有播放终态。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal

from behavior.models import (
    AttemptFinished,
    AttemptRejected,
    AudioReady,
    ControlAction,
    NoAction,
    NotExecuted,
    RecognitionObserved,
    ReplySettings,
)
from channels.models import (
    ActionOutcome,
    PlaybackRequest,
    PlaybackResult,
    RoomTarget,
    SendText,
    SessionRef,
)
from context.models import ConfirmedTurn, ContextRequest, Observation, OutputRecord
from dialogue import audio_output
from dialogue.models import AudioInput, ControlInput

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlowResult:
    user_text: str = ""
    reply_text: str = ""
    outcome: PlaybackResult | None = None
    control: ControlAction | None = None
    reason: str = ""
    text_outcome: ActionOutcome | None = None
    text_reason: str = ""

    @property
    def subtitle(self) -> dict:
        return subtitle_receipt(self.text_outcome, self.text_reason)


def subtitle_receipt(outcome: ActionOutcome | None, reason: str) -> dict:
    if outcome is None:
        return {"status": "not_executed", "reason": reason}
    return {"status": outcome.status, "reason": outcome.reason, "action_id": outcome.action_id,
            "message_id": outcome.message_id, "parent_action_id": outcome.parent_action_id,
            "target": {"area": outcome.target.area, "channel": outcome.target.channel} if outcome.target else None}


async def commit_output(context, record: OutputRecord):
    """拥有不可取消的SQLite线程直到收尾；流程退出后才允许关闭上下文。"""
    task = asyncio.create_task(asyncio.to_thread(context.record_output, record), name='app-context-commit')
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            break
    if cancelled is not None:
        try:
            task.result()
        except Exception:
            logger.exception('event=context.commit.failed_after_cancellation action_id=%s', record.action_id)
        raise cancelled
    return task.result()


async def record_expression_outputs(*, context, channel, session, prepared, outcome, evidence,
                                    user_text, user_key, input_ref="", current=lambda:True,
                                    reply_text_to_channel=False, subtitle_target: RoomTarget | None=None,
                                    interrupted=False):
    """独立记录音频与可选文字事实；任何一项成功都不伪造另一项结果。"""
    audio_confirmed = (outcome.status == "completed" and evidence.status == "completed"
                       and bool(evidence.text.strip()))
    text_reason = ("disabled" if not reply_text_to_channel else
                   "target_unavailable" if subtitle_target is None else
                   "superseded" if not current() or interrupted else
                   "generation_unconfirmed" if evidence.status != "completed" or not evidence.text.strip() else "")
    text_planned = not text_reason
    # Observations already stored the input. If text is the only confirmed output,
    # its own record carries the ordinary external-memory commit, avoiding duplicate user turns.
    audio_input = "" if text_planned and not audio_confirmed else user_text
    audio_turn = ConfirmedTurn(audio_input, evidence.text if audio_confirmed else "",
        "completed" if audio_confirmed else "interrupted" if interrupted else "failed", user_key,
        f"{session.room_target.area}/{session.room_target.channel}")
    await commit_output(context, OutputRecord(outcome.action_id, session, audio_turn, outcome,
                                              prepared.context_ref, input_ref))
    if not text_planned:
        if reply_text_to_channel:
            logger.info("event=voice.subtitle.not_executed reason=%s session_id=%s parent_action_id=%s",
                        text_reason, session.session_id, outcome.action_id)
        return None, text_reason
    assert subtitle_target is not None
    command = SendText(outcome.action_id + ":text", session, subtitle_target, evidence.text, outcome.action_id)
    text_outcome = None
    try:
        text_outcome = await channel.execute(command)
        logger.info("event=voice.subtitle.finished status=%s reason=%s session_id=%s action_id=%s parent_action_id=%s",
                    text_outcome.status, text_outcome.reason, session.session_id, command.action_id, outcome.action_id)
        return text_outcome, text_outcome.reason
    finally:
        if not isinstance(text_outcome, ActionOutcome):
            text_outcome = channel.outcome(command.action_id)
        if isinstance(text_outcome, ActionOutcome):
            text_confirmed = text_outcome.status == "completed"
            text_turn = (ConfirmedTurn("", "", "silent", user_key,
                f"{session.room_target.area}/{session.room_target.channel}") if audio_confirmed else
                ConfirmedTurn(user_text, evidence.text if text_confirmed else "",
                    "completed" if text_confirmed else "failed", user_key,
                    f"{session.room_target.area}/{session.room_target.channel}"))
            await commit_output(context, OutputRecord(command.action_id, session, text_turn,
                text_outcome, prepared.context_ref, input_ref))


async def run_cascade(*, backend, context, behavior, channel,
                      session: SessionRef, trigger_id: str, user_key: str,
                      pcm: bytes, sample_rate: int, limit: int,
                      settings: ReplySettings, interpret_controls: bool,
                      current, on_playing, observations=None, reply_text_to_channel: bool = False,
                      subtitle_target: RoomTarget | None = None, recognition=None) -> FlowResult:
    """取消必结尝试；执行后的失败使用渠道事实，执行前失败使用NotExecuted。"""
    revision = context.snapshot(session.room_target).revision
    trigger = recognition.trigger if recognition else AudioReady(trigger_id, session, user_key, revision)
    if trigger.session != session or trigger.user_key != user_key or trigger.trigger_id != trigger_id:
        return FlowResult(reason="stale_recognition")
    permit = recognition.permit if recognition else behavior.admit(trigger, limit)
    if isinstance(permit, AttemptRejected):
        return FlowResult(reason=permit.reason)
    started = time.monotonic()
    timings = {}
    trace_reason = "error"
    end_reason = "failed"
    intent = job = None
    execution_started = False
    feedback_sent = False
    commit_started = False
    prepared = recognized = audio = None
    try:
        recognized = (await recognition.stream.result() if recognition else await backend.recognize(
            AudioInput(trigger_id, session, pcm, sample_rate, user_key, permit.attempt_id), permit))
        timings["asr"] = recognized.elapsed_ms
        if recognition:
            timings["asr_finalize"] = recognized.finalize_ms
        logger.info("event=voice.asr.completed input_chars=%d audio_seconds=%.1f session_id=%s attempt_id=%s",
                    len(recognized.text), len(pcm)/2/sample_rate, session.session_id, permit.attempt_id)
        if not current():
            end_reason = "superseded"
            return FlowResult(reason=end_reason)
        if recognized.status not in {"recognized", "completed", "no_speech"}:
            return FlowResult(reason=recognized.reason or "recognition_failed")
        revision = context.observe(Observation(
            trigger_id, session, session.session_id, permit.attempt_number,
            "recognized", text=recognized.text, user_key=user_key, input_ref=trigger_id))
        control = None
        if interpret_controls and recognized.text:
            interpreted = await backend.interpret_control(ControlInput(
                recognized.text, trigger_id, session, user_key, permit.attempt_id))
            if not current():
                end_reason = "superseded"
                return FlowResult(reason=end_reason)
            timings["intent"] = interpreted.elapsed_ms
            if interpreted.status not in {"candidate", "no_control"}:
                end_reason = "classification_" + interpreted.status
                return FlowResult(user_text=recognized.text, reason=end_reason)
            control = interpreted.candidate
        decision = behavior.decide(RecognitionObserved(
            trigger, permit, recognized.text, revision, len(pcm)/2/sample_rate*1000, control), settings)
        if isinstance(decision, NoAction):
            trace_reason = decision.reason
            end_reason = decision.reason
            return FlowResult(user_text=recognized.text, reason=decision.reason)
        if isinstance(decision, ControlAction):
            trace_reason = "control"
            end_reason = "control"
            return FlowResult(user_text=recognized.text, control=decision)
        intent = decision.intent
        trace_reason = "reply"
        prepared = await context.prepare_context(ContextRequest(
            session, user_key, trigger_id, known_revision=intent.context_version))
        if not current():
            end_reason = "superseded"
            return FlowResult(reason=end_reason)
        screen = None
        capabilities = getattr(backend, "capabilities", None)
        if capabilities is not None and capabilities().image_input:
            capture = await channel.capture_screen(session)
            logger.info("event=voice.screen.captured status=%s session_id=%s input_ref=%s",
                        capture.status, session.session_id, trigger_id)
            if not current():
                end_reason = "superseded"
                return FlowResult(reason=end_reason)
            screen = capture.frame
        job = (backend.generate(intent, prepared, tools=context.tools(prepared), screen=screen)
               if screen is not None else backend.generate(intent, prepared, tools=context.tools(prepared)))
        audio = audio_output(job)
        on_playing(True)
        execution_started = True
        outcome = await channel.play(PlaybackRequest(
            intent.action_id, session, job.generation_id, intent.deadline), audio.frames())
        behavior.feedback(outcome)
        feedback_sent = True
        if outcome.status != "completed":
            logger.warning("event=voice.playback.failed 推流失败 reason=%s session_id=%s action_id=%s",
                           outcome.reason, session.session_id, outcome.action_id)
        evidence = audio.evidence()
        timings.update(dict(evidence.timings_ms))
        end_reason = outcome.status
        commit_started = True
        text_outcome, text_reason = await record_expression_outputs(context=context, channel=channel,
            session=session, prepared=prepared, outcome=outcome, evidence=evidence,
            user_text=recognized.text, user_key=user_key, input_ref=trigger_id, current=current,
            reply_text_to_channel=reply_text_to_channel, subtitle_target=subtitle_target)
        return FlowResult(recognized.text, evidence.text, outcome,
                          text_outcome=text_outcome, text_reason=text_reason)
    except asyncio.CancelledError:
        if intent is not None and execution_started:
            result = channel.playback(intent.action_id)
            if isinstance(result, PlaybackResult):
                if not feedback_sent:
                    behavior.feedback(result)
                    feedback_sent = True
                if not commit_started and prepared is not None and recognized is not None:
                    evidence = audio.evidence() if audio is not None else None
                    confirmed = (result.status == 'completed' and evidence is not None
                                 and evidence.status == 'completed' and bool(evidence.text.strip()))
                    turn = ConfirmedTurn(recognized.text, evidence.text if evidence is not None and confirmed else '',
                        'completed' if confirmed else 'interrupted', user_key,
                        f'{session.room_target.area}/{session.room_target.channel}')
                    commit_started = True
                    await commit_output(context, OutputRecord(intent.action_id, session,
                        turn, result, prepared.context_ref, trigger_id))
        end_reason = 'interrupted'
        raise
    finally:
        try:
            on_playing(False)
        finally:
            try:
                if job is not None:
                    await job.aclose()
            finally:
                if intent is not None and not feedback_sent:
                    actual = channel.playback(intent.action_id) if execution_started else None
                    behavior.feedback(actual if isinstance(actual, PlaybackResult) else
                        NotExecuted(intent.action_id, 'execution' if execution_started else 'generation', end_reason))
                behavior.finish_attempt(AttemptFinished(permit, end_reason, time.monotonic()))
                if recognition:
                    recognition.finished = True
                if observations is not None:
                    observations.record(trigger_id=trigger_id, session_id=session.session_id,
                        backend=backend.name, reason=trace_reason, outcome=end_reason,
                        elapsed_ms=(time.monotonic()-started)*1000, timings_ms=timings,
                        timing_mode='generation_overlap' if backend.capabilities().streaming_utterance else 'stages')


async def run_expression(*, backend, context, channel, session, text, kind: Literal["fixed", "announcement"]="fixed", user_key="api",
                         current, timeout: float=60, announcement_kind: Literal["enter", "leave"]="enter",
                         reply_text_to_channel: bool=False, subtitle_target: RoomTarget | None=None):
    from uuid import uuid4

    from behavior.models import SpeechIntent

    prepared = await context.prepare_context(ContextRequest(session, user_key))
    if not current():
        return FlowResult(reason="superseded")
    action_id = uuid4().hex
    intent = SpeechIntent(action_id, action_id, session, action_id, prepared.version,
                          time.monotonic() + timeout, text, user_key, kind=kind, announcement_kind=announcement_kind)
    job = backend.generate(intent, prepared, tools=context.tools(prepared))
    audio = audio_output(job)
    try:
        outcome = await channel.play(PlaybackRequest(action_id, session, job.generation_id, intent.deadline), audio.frames())
        evidence = audio.evidence()
        text_outcome, text_reason = await record_expression_outputs(context=context, channel=channel,
            session=session, prepared=prepared, outcome=outcome, evidence=evidence,
            user_text="", user_key=user_key, current=current,
            reply_text_to_channel=reply_text_to_channel, subtitle_target=subtitle_target)
        return FlowResult(reply_text=evidence.text, outcome=outcome,
                          text_outcome=text_outcome, text_reason=text_reason)
    finally:
        await job.aclose()
