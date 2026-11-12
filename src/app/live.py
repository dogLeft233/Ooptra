"""持续会话流程：事件单消费，行为许可映射、渠道执行与事务记录。"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from behavior.models import (
    ControlAction,
    ControlProposal,
    DeferLive,
    LiveProposal,
    NoAction,
    NotExecuted,
    Speak,
    SpeechIntent,
    resolve_volume_command,
)
from channels.models import ActionOutcome, PlaybackRequest, PlaybackResult, SetOutput
from context.models import ContextRequest, Observation, ObservationFailed
from dialogue import audio_output
from dialogue.models import Allow, AudioInput, ControlResolution, Hold, LiveSessionRequest, Reject

logger = logging.getLogger(__name__)


class LiveFlow:
    def __init__(self, *, backend, context, behavior, channel, session, current,
                 settings, on_state, on_leave, output_state, observations=None, subtitles_enabled=lambda:False, subtitle_target=lambda:None, on_subtitle=lambda _outcome, _reason:None):
        self.backend, self.context, self.behavior, self.channel = backend, context, behavior, channel
        self.session, self.current = session, current
        self.settings, self.on_state, self.on_leave, self.output_state = settings, on_state, on_leave, output_state
        self.observations = observations
        self.subtitles_enabled, self.subtitle_target = subtitles_enabled, subtitle_target
        self.on_subtitle = on_subtitle
        self.live = None
        self._reader = None
        self._outputs: set[asyncio.Task] = set()
        self._actions: set[str] = set()
        self._metadata = {}
        self._prepared = {}
        self._sequence = 0
        self._closed = False
        self._manual = {}
        self._control_commands = {}
        self._control_tasks = {}
        self._cancelled_controls = set()
        self._active_turns = set()
        self._failures = []
        self._preparing = 0

    async def start(self):
        prepared = await self.context.prepare_context(ContextRequest(self.session))
        self.behavior.start_session(self.session)
        self.live = await self.backend.open_live(LiveSessionRequest(self.session, prepared), tools=self.context.tools(prepared))
        self._initial = prepared
        self._reader = asyncio.create_task(self._events(), name="app-live-events")
        self._reader.add_done_callback(self._observe)

    def _observe(self, done):
        if not done.cancelled() and done.exception() is not None:
            error = done.exception()
            if isinstance(error, ObservationFailed):
                # 输入提交已有明确失败终态；它不表示模型/播放资源未收尾，
                # 不能再作为关闭失败阻止实际退房。
                logger.error("event=voice.live.flow_failed session_id=%s input_ref=%s reason=context_observation",
                             error.session.session_id, error.input_ref)
                return
            self._failures = [*self._failures[-7:], type(error).__name__]
            logger.error("event=voice.live.flow_failed session_id=%s", self.session.session_id,
                         exc_info=error)

    @property
    def busy(self):
        return bool(self._outputs) or self._preparing > 0

    async def send_input(self, user_key, pcm, rate):
        if self.live is not None and self.current() and not self._closed:
            await self.live.send_input(AudioInput(uuid4().hex, self.session, pcm, rate, user_key))

    def _key(self, event):
        return f"{event.proposal.live_session_id}:{event.proposal.turn_id}"

    async def _prepare(self, event, *, complete=False):
        self._preparing += 1
        try:
            return await self._prepare_context(event, complete=complete)
        finally:
            self._preparing -= 1

    async def _prepare_context(self, event, *, complete=False):
        assert self.live is not None
        key = self._key(event)
        if key not in self._metadata and len(self._metadata) >= 128:
            removable = next((old for old in self._metadata if old not in self._active_turns), None)
            if removable is not None:
                self._metadata.pop(removable, None)
                self._prepared.pop(removable, None)
        self._metadata[key] = event
        self._sequence += 1
        if not event.internal:
            if self.observations is not None:
                self.observations.trace(self.session, key, event.user_key, "recognized", text=event.text,
                    text_final=complete, reason="awaiting_decision")
            await self.context.observe_async(Observation(f"{key}:{self._sequence}", self.session,
                self.live.live_session_id, self._sequence, "recognized", event.text,
                event.user_key, key, complete=complete))
        prepared = await self.context.prepare_context(ContextRequest(self.session, event.user_key, key))
        if not self.current() or self._closed:
            return None
        receipt = await self.live.update_context(prepared, tools=self.context.tools(prepared))
        if receipt.status != 'applied':
            return None
        self._prepared[key] = prepared
        return prepared

    async def _events(self):
        assert self.live is not None
        async for event in self.live.events():
            if not self.current() or self._closed:
                break
            if event.kind in {"proposed", "updated"}:
                # 同一网络包可能已把提议修订；过时事件不能先占用一次性意图。
                latest = self.live.snapshot().current_proposal
                if latest is not None and latest.turn_id == event.proposal.turn_id and latest.revision > event.proposal.revision:
                    continue
                prepared = await self._prepare(event)
                if prepared is None:
                    await self.live.resolve_turn(event.proposal, Reject('context_superseded'))
                    continue
                decision = self.behavior.decide_live(LiveProposal(
                    self._key(event), self.session, event.proposal.revision, event.text,
                    event.user_key, prepared.version, event.deadline, event.internal), self.settings())
                if isinstance(decision, Speak):
                    receipt = await self.live.resolve_turn(event.proposal, Allow(decision.intent))
                    if receipt.status not in {"applied", "accepted", "duplicate"}:
                        self.behavior.feedback(NotExecuted(decision.intent.action_id, "permission", receipt.status))
                elif isinstance(decision, DeferLive):
                    await self.live.resolve_turn(event.proposal, Hold(decision.until))
                else:
                    assert isinstance(decision, NoAction)
                    await self.live.resolve_turn(event.proposal, Reject(decision.reason))
                    if self.observations is not None:
                        self.observations.trace(self.session, self._key(event), event.user_key, "finished",
                            decision="silent", reason=decision.reason, outcome="skipped")
                        self.observations.record(trigger_id=self._key(event), session_id=self.session.session_id,
                            backend=self.backend.name, reason=decision.reason, outcome="skipped",
                            elapsed_ms=0, timing_mode="live_overlap")
            elif event.kind == "output_ready":
                if len(self._outputs) >= 8:
                    await event.job.aclose()
                    self.behavior.feedback(NotExecuted(event.action_id, "capacity", "busy"))
                    continue
                task = asyncio.create_task(self._play(event), name="app-live-playback")
                self._outputs.add(task)
                task.add_done_callback(self._observe)
                task.add_done_callback(self._outputs.discard)
            elif event.kind == "turn_finished" and event.proposal is not None:
                await self._prepare(event, complete=True)
                if self.observations is not None and not event.internal and not event.action_id:
                    # 无许可/无播放的轮次以明确生成结束证据关闭诊断；有行动的轮次
                    # 仍由渠道播放结果提供终态，不能把生成完成当作播放成功。
                    self.observations.trace(self.session, self._key(event), event.user_key, "finished",
                        text=event.text, decision="silent", reason=event.reason or "not_executed",
                        outcome="not_executed")
            elif event.kind == "control_proposed":
                if event.tool_ref in self._control_tasks:
                    continue
                if len(self._outputs) >= 8:
                    await self.live.resolve_control(event.tool_ref,
                        ControlResolution('not_executed', reason='capacity'))
                    continue
                task = asyncio.create_task(self._control(event), name="app-live-control")
                self._outputs.add(task)
                self._control_tasks[event.tool_ref] = task
                def control_finished(done, reference=event.tool_ref):
                    self._control_tasks.pop(reference, None)
                    self._cancelled_controls.discard(reference)
                task.add_done_callback(control_finished)
                task.add_done_callback(self._observe)
                task.add_done_callback(self._outputs.discard)
            elif event.kind == 'control_cancelled':
                task = self._control_tasks.get(event.tool_ref)
                if task is not None:
                    self._cancelled_controls.add(event.tool_ref)
                    task.cancel()

    async def _play(self, event):
        key = self._key(event)
        job, action_id = event.job, event.action_id
        audio = audio_output(job)
        prepared = self._prepared.get(key, self._initial)
        subtitles_enabled, subtitle_target = self.subtitles_enabled(), self.subtitle_target()
        self._actions.add(action_id)
        self._active_turns.add(key)
        self.on_state(True, '', '', '')
        outcome = None
        confirmed = False
        text_outcome, text_reason = None, "not_executed"
        failure = 'playback_failed'
        try:
            outcome = await self.channel.play(PlaybackRequest(action_id, self.session,
                job.generation_id, time.monotonic() + 60), audio.frames())
        except asyncio.CancelledError:
            failure = 'cancelled'
            raise
        finally:
            try:
                if not isinstance(outcome, PlaybackResult):
                    outcome = self.channel.playback(action_id)
                if isinstance(outcome, PlaybackResult):
                    self.behavior.feedback(outcome)
                    evidence = audio.evidence()
                    confirmed = bool(outcome.status == 'completed' and evidence.status == 'completed'
                                     and evidence.text.strip())
                    if outcome.status != 'completed':
                        logger.warning('event=voice.playback.failed 推流失败 reason=%s session_id=%s action_id=%s',
                            outcome.reason, self.session.session_id, action_id)
                    if self.observations is not None:
                        final_trace = self._metadata.get(key, event)
                        if not final_trace.internal:
                            self.observations.trace(self.session, key, final_trace.user_key, "finished",
                                decision="speak", reason="reply", outcome=outcome.status,
                                elapsed_ms=dict(evidence.timings_ms).get('generation_total', 0))
                        self.observations.record(trigger_id=key, session_id=self.session.session_id,
                            backend=self.backend.name, reason='reply', outcome=outcome.status,
                            elapsed_ms=dict(evidence.timings_ms).get('generation_total', 0),
                            timings_ms=dict(evidence.timings_ms), timing_mode='live_overlap')
                    final = self._metadata.get(key, event)
                    user_text = '' if final.internal else final.text
                    from app.flows import record_expression_outputs
                    text_outcome, text_reason = await record_expression_outputs(
                        context=self.context, channel=self.channel, session=self.session,
                        prepared=prepared, outcome=outcome, evidence=evidence, user_text=user_text,
                        user_key=final.user_key, input_ref=key, current=self.current,
                        reply_text_to_channel=subtitles_enabled, subtitle_target=subtitle_target,
                        interrupted=failure == "cancelled")
                    self.on_subtitle(text_outcome, text_reason)
                    if subtitles_enabled and text_reason:
                        logger.info("event=voice.subtitle.not_completed reason=%s session_id=%s action_id=%s",
                                    text_reason, self.session.session_id, action_id)
                    self.on_state(False, user_text, evidence.text, outcome.status)
                else:
                    self.behavior.feedback(NotExecuted(action_id, 'execution', failure))
            finally:
                future = self._manual.pop(action_id, None)
                if future is not None and not future.done():
                    from app.flows import subtitle_receipt
                    future.set_result({'ok': confirmed, 'text': audio.evidence().text, 'error': failure,
                                       'subtitle': subtitle_receipt(text_outcome, text_reason)})
                self._actions.discard(action_id)
                self._active_turns.discard(key)
                await job.aclose()
                if self.current():
                    self.on_state(False, '', '', '')

    async def _control(self, event):
        assert self.live is not None
        if (event.tool_ref is None or event.candidate is None or not self.current()
                or self._closed or event.tool_ref in self._cancelled_controls):
            return
        trigger_id = f'{event.tool_ref.live_session_id}:{event.tool_ref.connection_generation}:{event.tool_ref.provider_tool_call_id}'
        decision = self.behavior.decide_control(ControlProposal(trigger_id, self.session, event.candidate), self.settings())
        if not isinstance(decision, ControlAction):
            await self.live.resolve_control(event.tool_ref, ControlResolution('rejected', reason=decision.reason))
            return
        command, action_id = decision.control, decision.action_id
        permission = self.live.claim_control(event.tool_ref, action_id)
        if permission.status not in {'accepted', 'duplicate'}:
            self.behavior.feedback(NotExecuted(action_id, 'control', permission.status))
            return
        execution = None
        cancelled = False
        feedback_sent = False
        departure_scheduled = False
        try:
            if command.leave:
                await self.live.resolve_control(event.tool_ref, ControlResolution('scheduled',
                    reason='farewell_then_leave'))
                if not self.current() or event.tool_ref in self._cancelled_controls:
                    return
                departure_scheduled = self.on_leave(action_id=action_id, expected_session=self.session)
                return
            if command.volume is not None:
                state = self.output_state()
                volume, muted = resolve_volume_command(command.volume, state['volume'])
                if action_id not in self._control_commands and len(self._control_commands) >= 128:
                    self._control_commands.pop(next(iter(self._control_commands)))
                command = self._control_commands.setdefault(action_id, SetOutput(action_id, self.session, volume, muted))
                if not self.current() or event.tool_ref in self._cancelled_controls:
                    return
                execution = asyncio.create_task(self.channel.execute(command), name='app-live-control-execution')
                # Once accepted, a platform write may already be committed. Cancellation
                # revokes the tool waiter, while we observe the owned channel action.
                while not execution.done():
                    try:
                        await asyncio.shield(execution)
                    except asyncio.CancelledError:
                        cancelled = True
                outcome = execution.result()
                self.behavior.feedback(outcome)
                feedback_sent = True
                if not cancelled and self.current() and event.tool_ref not in self._cancelled_controls:
                    actual = self.output_state()
                    await self.live.resolve_control(event.tool_ref, ControlResolution(
                        'executed', outcome, actual['volume'], actual['muted']))
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            actual = self.channel.outcome(action_id)
            if isinstance(actual, ActionOutcome):
                self.behavior.feedback(actual)
                feedback_sent = True
                if self.current() and event.tool_ref not in self._cancelled_controls:
                    await self.live.resolve_control(event.tool_ref, ControlResolution('executed', actual))
            else:
                if self.current() and event.tool_ref not in self._cancelled_controls:
                    await self.live.resolve_control(event.tool_ref, ControlResolution('not_executed', reason='execution_failed'))
            logger.exception('event=voice.live.control_failed action_id=%s', action_id)
        finally:
            if not feedback_sent and not departure_scheduled:
                self.behavior.feedback(NotExecuted(action_id, 'control', 'cancelled' if cancelled else 'not_executed'))
        if cancelled:
            raise asyncio.CancelledError

    async def speak(self, text, *, timeout=60, wait=False):
        assert self.live is not None
        if self._closed or not self.current() or len(self._manual) >= 8:
            return {'ok': False, 'error': 'busy_or_closed'}
        prepared = await self.context.prepare_context(ContextRequest(self.session, 'api'))
        if self._closed or not self.current():
            return {'ok': False, 'error': 'superseded'}
        updated = await self.live.update_context(prepared, tools=self.context.tools(prepared))
        if updated.status != 'applied':
            return {'ok': False, 'error': 'context_superseded'}
        action_id = uuid4().hex
        intent = SpeechIntent(action_id, action_id, self.session, action_id,
            prepared.version, time.monotonic()+timeout, text, "api", kind="fixed")
        future = asyncio.get_running_loop().create_future()
        self._manual[action_id] = future
        try:
            receipt = await self.live.speak(intent)
        except BaseException:
            pending = self._manual.pop(action_id, None)
            if pending is not None:
                pending.cancel()
            if action_id in self._actions:
                await self.channel.cancel_playback(action_id)
            else:
                self.behavior.feedback(NotExecuted(action_id, "permission", "send_failed"))
            raise
        if receipt.status not in {"applied", "accepted"}:
            self._manual.pop(action_id, None)
            return {"ok": False, "error": receipt.status}
        if wait:
            try:
                return await asyncio.wait_for(future, timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                self._manual.pop(action_id, None)
                await self.interrupt()
                raise
        return {'ok': True, 'chars': len(text), 'mode': 'live'}

    async def interrupt(self):
        for action_id in tuple(self._actions):
            await self.channel.cancel_playback(action_id)
        if self.live is not None:
            await self.live.interrupt()

    async def close(self):
        self._closed = True
        current = asyncio.current_task()
        reader, self._reader = self._reader, None
        if reader is not None and reader is not current:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        outputs = tuple(task for task in self._outputs if task is not current)
        for task in outputs:
            task.cancel()
        await asyncio.gather(*outputs, return_exceptions=True)
        if self.live is not None:
            await self.live.aclose()
        for future in self._manual.values():
            if not future.done():
                future.cancel()
        self._manual.clear()
        self._metadata.clear()
        self._prepared.clear()
        self._control_commands.clear()
        if self._failures:
            raise RuntimeError('Live流程存在未确认的失败：' + ','.join(self._failures))
