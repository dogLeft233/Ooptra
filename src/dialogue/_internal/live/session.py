"""持续模型协议会话：reader只投递有界数据；行为许可与播放在外部。"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from uuid import uuid4

from behavior.models import VoiceControlDecision, parse_volume_command
from dialogue._internal.output import ExpressionJob
from dialogue.models import (
    Allow,
    AudioInput,
    ControlResolution,
    Hold,
    LiveEvent,
    LiveSessionRequest,
    LiveSnapshot,
    ProposalRef,
    Reject,
    ResolveReceipt,
    ToolRef,
)

logger = logging.getLogger(__name__)


@dataclass
class _Turn:
    ref: ProposalRef
    deadline: float
    expression_deadline: float
    context: Any = None
    tools: Any = None
    user_text: str = ""
    text: str = ""
    speaker: str = ""
    action_id: str = ""
    state: str = "pending"
    provider_done: asyncio.Event = field(default_factory=asyncio.Event)
    audio: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(128))
    buffered_bytes: int = 0
    job: ExpressionJob | None = None
    timer: asyncio.Task | None = None
    internal: bool = False
    input_ready: asyncio.Event = field(default_factory=asyncio.Event)
    authorization_ready: asyncio.Event = field(default_factory=asyncio.Event)
    input_timer: asyncio.Task | None = None
    authorized_context: Any = None
    authorized_tools: Any = None
    rejection_reason: str = ""
    input_reported: bool = False


class LiveSession:
    """固定渠道/上下文绑定，控制事件单订阅，未许可音频绝不离开会话。

    控制队列128，音频累计预算来自LiveLimits；溢出fail-closed并停止旧连接。
    reader不等待许可、播放或工具执行，aclose回收当前任务与连接。
    """

    def __init__(self, backend, request: LiveSessionRequest) -> None:
        self._backend, self.request = backend, request
        self._limits = request.limits or backend.limits
        self.live_session_id = uuid4().hex
        self._events: asyncio.Queue[LiveEvent] = asyncio.Queue(128)
        self._claimed = False
        self._closed = asyncio.Event()
        self._state: Literal["opening", "ready", "failed", "closed"] = "opening"
        self._reason = ""
        self._turn: _Turn | None = None
        self._last: _Turn | None = None
        self._turns: dict[str, _Turn] = {}
        self._tasks: set[asyncio.Task] = set()
        self._controls: dict[str, tuple[ToolRef, dict, ControlResolution | None]] = {}
        self._memory_jobs: dict[str, asyncio.Task] = {}
        self._memory_results: dict[str, dict] = {}
        self._memory_calls: dict[str, dict] = {}
        self._claimed_controls: dict[str, str] = {}
        self._cancelled_controls: set[str] = set()
        self._speaker = ""
        self._speakers: set[str] = set()
        self._tools = backend._tools
        self._sequence = 0
        self._generation = 1
        self._writer = asyncio.Lock()

    async def start(self) -> None:
        self._backend._task_session = self
        try:
            await self._backend.start_session()
            self._state = "ready"
        except BaseException:
            await self.aclose()
            raise

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        def observe(done):
            self._tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.warning("event=dialogue.live.task_failed reason=transport_or_tool")
        task.add_done_callback(observe)
        return task

    def _publish(self, event: LiveEvent) -> None:
        if self._closed.is_set():
            return
        if self._events.full():
            self._fail("event_delivery_overflow")
            return
        self._events.put_nowait(event)

    def _fail(self, reason: str) -> None:
        if self._state in {"closed", "failed"}:
            return
        self._state, self._reason = "failed", reason
        if self._turn is not None:
            turn = self._turn
            unfinished = turn.state in {"pending", "allowed", "rejected"} and not turn.input_reported
            turn.state = "failed"
            turn.provider_done.set()
            turn.input_ready.set()
            if unfinished:
                turn.input_reported = True
                self._publish(LiveEvent("turn_finished", self.request.session, turn.ref,
                    turn.user_text, turn.speaker, action_id=turn.action_id, reason=reason,
                    internal=turn.internal))
        self._closed.set()
        # 独立关闭任务，不能让provider reader取消并await自己。
        self._spawn(self._backend.close())

    async def send_input(self, input: AudioInput) -> ResolveReceipt:
        if input.session != self.request.session:
            return ResolveReceipt("stale", reason="session_mismatch")
        if self._state != "ready":
            return ResolveReceipt("unavailable", reason=self._reason or self._state)
        if input.user_key:
            self._speakers.add(input.user_key)
        self._speaker = next(iter(self._speakers)) if len(self._speakers) == 1 else ""
        if len(self._speakers) > 2:
            self._speakers = {"__mixed__", "__unknown__"}
        # 音频输入不经过旧provider策略/自动重连路径。
        async with self._writer:
            await self._backend._send({"realtimeInput": {"audio": {
                "data": base64.b64encode(input.pcm16).decode("ascii"),
                "mime_type": f"audio/pcm;rate={input.sample_rate}"}}})
        return ResolveReceipt("accepted")

    async def speak(self, intent) -> ResolveReceipt:
        """显式台词使用同一许可/输出协议，不能额外创建播放器。"""
        if intent.session != self.request.session or self._state != "ready":
            return ResolveReceipt("stale")
        if self._turn is not None and self._turn.state not in {"completed", "rejected", "expired"}:
            return ResolveReceipt("conflict")
        turn = self._new_turn(internal=True)
        result = await self.resolve_turn(turn.ref, Allow(intent))
        try:
            async with self._writer:
                await self._backend._send({"realtimeInput": {"text": intent.text}})
        except BaseException:
            turn.state = "cancelled"
            while not turn.audio.empty():
                turn.audio.get_nowait()
            turn.buffered_bytes = 0
            if turn.job is not None:
                await turn.job.aclose()
            self._publish(LiveEvent("turn_finished", self.request.session, turn.ref,
                action_id=turn.action_id, reason="cancelled", internal=True))
            raise
        return result

    def _new_turn(self, *, internal: bool = False) -> _Turn:
        self._sequence += 1
        now = time.monotonic()
        turn = _Turn(ProposalRef(self.live_session_id, str(self._sequence), 0),
                     now + self._limits.late_transcription_seconds, now + 60,
                     context=self.request.context, tools=self._tools,
                     speaker=self._speaker, internal=internal)
        self._turn = turn
        self._turns[turn.ref.turn_id] = turn
        while len(self._turns) > 128:
            first = next(iter(self._turns))
            if self._turns[first].state in {"pending", "allowed"}:
                break
            del self._turns[first]
        if not internal:
            self._propose(turn, "proposed")
        turn.timer = self._spawn(self._expire(turn))
        return turn

    def _propose(self, turn: _Turn, kind: Literal["proposed", "updated"]) -> None:
        self._publish(LiveEvent(kind, self.request.session, turn.ref, turn.user_text,
                               turn.speaker, turn.deadline, internal=turn.internal))

    def _finalize_input(self, turn: _Turn) -> None:
        if not turn.input_ready.is_set():
            turn.input_ready.set()
            if not turn.provider_done.is_set() and turn.state == "allowed":
                self._publish(LiveEvent("input_finalized", self.request.session, turn.ref,
                    turn.user_text, turn.speaker, action_id=turn.action_id, internal=turn.internal))
        if turn.provider_done.is_set() and turn.state in {"allowed", "rejected"} and not turn.input_reported:
            reason = turn.rejection_reason if turn.state == "rejected" else "completed"
            if turn.state == "allowed":
                turn.state = "completed"
            turn.input_reported = True
            if turn.timer is not None and turn.timer is not asyncio.current_task():
                turn.timer.cancel()
            self._publish(LiveEvent("turn_finished", self.request.session, turn.ref,
                turn.user_text, turn.speaker, action_id=turn.action_id, reason=reason,
                internal=turn.internal))

    def _schedule_input_finalization(self, turn: _Turn) -> None:
        if turn.input_ready.is_set():
            self._finalize_input(turn)
        elif turn.input_timer is None:
            async def finalize():
                await asyncio.sleep(self._limits.late_transcription_seconds)
                self._finalize_input(turn)
            turn.input_timer = self._spawn(finalize())

    async def authorize_input(self, ref: ProposalRef, prepared, *, tools=None) -> ResolveReceipt:
        """只补交同轮定稿输入的工具授权，不替换已批准表达的上下文。"""
        turn = self._turns.get(ref.turn_id)
        if (self._closed.is_set() or ref.live_session_id != self.live_session_id or turn is None
                or ref != turn.ref or not turn.input_ready.is_set()):
            return ResolveReceipt("stale")
        if turn.state not in {"allowed", "completed"}:
            turn.authorization_ready.set()
            return ResolveReceipt("stale")
        if prepared is None or prepared.context_ref != turn.context.context_ref:
            turn.authorization_ready.set()
            return ResolveReceipt("stale")
        expected_input = f"{self.live_session_id}:{turn.ref.turn_id}"
        if any(grant.input_ref != expected_input or grant.context_ref != prepared.context_ref
               for grant in prepared.authorized_write_grants):
            turn.authorization_ready.set()
            return ResolveReceipt("stale")
        turn.authorized_context, turn.authorized_tools = prepared, tools
        turn.authorization_ready.set()
        return ResolveReceipt("applied")

    async def _expire(self, turn: _Turn) -> None:
        while turn.state in {"pending", "allowed", "rejected"}:
            await asyncio.sleep(max(0, turn.deadline - time.monotonic()))
            if time.monotonic() < turn.deadline:
                continue
            if turn.state == "pending":
                boundary_known = turn.provider_done.is_set()
                turn.state = "expired"
                turn.provider_done.set()
                self._publish(LiveEvent("turn_finished", self.request.session, turn.ref,
                    turn.user_text, turn.speaker, reason="expired", internal=turn.internal))
                if not boundary_known:
                    self._fail("unresolved_turn_boundary")
            elif turn.state in {"allowed", "rejected"} and not turn.provider_done.is_set():
                turn.state = "failed"
                turn.provider_done.set()
                self._fail("turn_deadline")
            return

    async def resolve_turn(self, ref: ProposalRef, permission: Allow | Hold | Reject) -> ResolveReceipt:
        if ref.live_session_id != self.live_session_id:
            return ResolveReceipt("stale")
        turn = self._turns.get(ref.turn_id)
        if turn is None:
            return ResolveReceipt("stale")
        if isinstance(permission, Allow) and turn.action_id:
            return ResolveReceipt("duplicate" if permission.intent.action_id == turn.action_id
                                  else "conflict", turn.action_id)
        if self._closed.is_set() or turn.state != "pending":
            return ResolveReceipt("stale", turn.action_id)
        if ref != turn.ref:
            return ResolveReceipt("stale", reason="proposal_revision")
        if time.monotonic() >= turn.deadline:
            return ResolveReceipt("expired")
        if isinstance(permission, Hold):
            if not time.monotonic() < permission.until <= turn.deadline:
                return ResolveReceipt("expired")
            # Hold只声明仍待信息，不能延长提议自己的硬截止。
            return ResolveReceipt("held")
        if isinstance(permission, Reject):
            turn.state = "rejected"
            turn.rejection_reason = permission.reason
            turn.authorization_ready.set()
            if turn.input_timer is not None:
                turn.input_timer.cancel()
                turn.input_timer = None
            if turn.timer is not None:
                turn.timer.cancel()
            turn.buffered_bytes = 0
            while not turn.audio.empty():
                turn.audio.get_nowait()
            turn.deadline = turn.expression_deadline
            turn.timer = self._spawn(self._expire(turn))
            if turn.provider_done.is_set():
                self._schedule_input_finalization(turn)
            return ResolveReceipt("rejected")
        intent = permission.intent
        if (intent.session != self.request.session or intent.deadline <= time.monotonic()
                or intent.context_version != turn.context.version):
            return ResolveReceipt("stale")
        turn.action_id, turn.state = intent.action_id, "allowed"
        turn.deadline = min(intent.deadline, turn.expression_deadline)
        if turn.provider_done.is_set():
            self._schedule_input_finalization(turn)
        async def produce(emit):
            try:
                while True:
                    if not turn.audio.empty():
                        pcm, rate = turn.audio.get_nowait()
                        turn.buffered_bytes -= len(pcm)
                        await emit("audio", pcm16=pcm, sample_rate=rate)
                    elif turn.provider_done.is_set():
                        break
                    else:
                        getter = asyncio.create_task(turn.audio.get())
                        done = asyncio.create_task(turn.provider_done.wait())
                        try:
                            await asyncio.wait({getter, done}, return_when=asyncio.FIRST_COMPLETED)
                            if getter.done():
                                pcm, rate = getter.result()
                                turn.buffered_bytes -= len(pcm)
                                await emit("audio", pcm16=pcm, sample_rate=rate)
                        finally:
                            for task in (getter, done):
                                if not task.done():
                                    task.cancel()
                            await asyncio.gather(getter, done, return_exceptions=True)
                if turn.state not in {"allowed", "completed"}:
                    raise RuntimeError("Live turn failed")
                await turn.input_ready.wait()
                await emit("text_final", text=turn.text)
            finally:
                if turn.state == "allowed" and not turn.provider_done.is_set():
                    turn.state = "cancelled"
                    self._publish(LiveEvent("turn_finished", self.request.session, turn.ref,
                        turn.user_text, turn.speaker, action_id=turn.action_id, reason="cancelled"))
        turn.job = ExpressionJob(intent.action_id, intent.session, produce,
                                 deadline=min(intent.deadline, turn.deadline))
        self._publish(LiveEvent("output_ready", self.request.session, turn.ref,
            turn.user_text, turn.speaker, action_id=turn.action_id, job=turn.job,
            internal=turn.internal))
        return ResolveReceipt("applied", turn.action_id)

    async def interrupt(self) -> None:
        """撤销本地当前轮次；保留连接并丢弃该轮迟到输出直到供应商边界。"""
        turn = self._turn
        if turn is None or turn.state not in {"pending", "allowed", "completed"}:
            return
        turn.state = "interrupted"
        if turn.timer is not None:
            turn.timer.cancel()
        if turn.input_timer is not None:
            turn.input_timer.cancel()
        self._finalize_input(turn)
        while not turn.audio.empty():
            turn.audio.get_nowait()
        turn.buffered_bytes = 0
        # provider_done只表达真实供应商边界；本地撤销不伪造新轮入口。
        if turn.job is not None:
            await turn.job.aclose()
        self._publish(LiveEvent("turn_finished", self.request.session, turn.ref,
            turn.user_text, turn.speaker, action_id=turn.action_id, reason="interrupted"))
        await self._backend.interrupt()

    async def update_context(self, prepared, *, tools=None) -> ResolveReceipt:
        """刷新同一来源的已观察情境；已许可轮次保留原授权/历史快照。"""
        original = self.request.context.context_ref
        ref = prepared.context_ref
        if (self._closed.is_set() or ref.session != original.session
                or ref.source_generation != original.source_generation
                or ref.binding_id != original.binding_id):
            return ResolveReceipt("stale", reason="context_source_changed")
        self.request = replace(self.request, context=prepared)
        self._tools = tools
        if self._turn is not None and self._turn.state == "pending":
            self._turn.context = prepared
            self._turn.tools = tools
        return ResolveReceipt("applied")

    async def _tool_response(self, call, result):
        async with self._writer:
            if not self._closed.is_set():
                await self._backend._send({"toolResponse": {"functionResponses": [{
                    "id": call["id"], "name": call["name"], "response": result}]}})

    async def _memory_tool(self, call, tools, prepared, speaker, turn=None):
        generation = self._generation
        key = call["id"]
        try:
            if (call["name"] == "astrbot_remember" and turn is not None and not turn.internal
                    and turn.state in {"pending", "allowed", "completed"}):
                self._schedule_input_finalization(turn)
                try:
                    await asyncio.wait_for(turn.authorization_ready.wait(),
                                           max(0, turn.expression_deadline-time.monotonic()))
                except asyncio.TimeoutError:
                    turn.authorization_ready.set()
                tools, prepared, speaker = turn.authorized_tools, turn.authorized_context, turn.speaker
                if turn.state not in {"allowed", "completed"}:
                    tools = None
            elif call["name"] == "astrbot_remember" and turn is not None:
                tools = None
            if tools is None:
                result = {"ok": False, "error": "Memory authorization unavailable"}
            else:
                try:
                    from dialogue._internal.memory_tools import execute_memory_tool
                    result = await execute_memory_tool(tools, prepared, call["name"], call.get("args"),
                                                       call_id=key, speaker=speaker)
                except asyncio.CancelledError:
                    self._memory_results[key] = {"ok": False, "status": "unknown",
                                                  "error": "Memory request cancelled"}
                    raise
                except Exception:
                    result = {"ok": False, "error": "Memory request unavailable"}
            self._memory_results[key] = result
            while len(self._memory_results) > 64:
                old = next(iter(self._memory_results))
                del self._memory_results[old]
                self._memory_calls.pop(old, None)
            if not self._closed.is_set() and generation == self._generation:
                await self._tool_response(call, result)
        finally:
            self._memory_jobs.pop(key, None)

    def claim_control(self, ref: ToolRef, action_id: str) -> ResolveReceipt:
        """执行前同步占用工具候选；provider已取消者不能进入渠道副作用。"""
        key = ref.provider_tool_call_id
        entry = self._controls.get(key)
        if (self._closed.is_set() or not action_id or ref.live_session_id != self.live_session_id
                or ref.connection_generation != self._generation or entry is None
                or entry[0] != ref or key in self._cancelled_controls):
            return ResolveReceipt("stale")
        previous = self._claimed_controls.get(key)
        if previous is not None:
            return ResolveReceipt("duplicate" if previous == action_id else "conflict", previous)
        if entry[2] is not None:
            return ResolveReceipt("stale")
        self._claimed_controls[key] = action_id
        return ResolveReceipt("accepted", action_id)

    async def resolve_control(self, ref: ToolRef, result: ControlResolution) -> ResolveReceipt:
        entry = self._controls.get(ref.provider_tool_call_id)
        if (self._closed.is_set() or ref.live_session_id != self.live_session_id
                or ref.connection_generation != self._generation or entry is None or entry[0] != ref
                or ref.provider_tool_call_id in self._cancelled_controls):
            return ResolveReceipt("stale")
        if entry[2] is not None:
            return ResolveReceipt("duplicate")
        self._controls[ref.provider_tool_call_id] = (ref, entry[1], result)
        await self._send_control(entry[1], result)
        return ResolveReceipt("applied", result.outcome.action_id if result.outcome is not None else "")

    async def _send_control(self, call, result):
        actual = result.outcome
        confirmed = result.status == "executed" and actual is not None and actual.status == "completed"
        payload = {"ok": confirmed, "status": actual.status if actual is not None else result.status,
                   "reason": result.reason}
        if confirmed and result.volume is not None:
            payload["volume"] = result.volume
        if confirmed and result.muted is not None:
            payload["muted"] = result.muted
        async with self._writer:
            if not self._closed.is_set():
                await self._backend._send({"toolResponse": {"functionResponses": [{
                    "id": call["id"], "name": call["name"], "response": payload}]}})

    async def handle(self, msg: dict) -> None:
        if self._closed.is_set():
            return
        if "setupComplete" in msg:
            self._backend._ready.set()
            return
        for key in (msg.get("toolCallCancellation") or {}).get("ids", []):
            memory_task = self._memory_jobs.get(key)
            if memory_task is not None:
                memory_task.cancel()
            entry = self._controls.get(key)
            if entry is not None:
                self._cancelled_controls.add(key)
                self._publish(LiveEvent("control_cancelled", self.request.session, tool_ref=entry[0]))
        calls = (msg.get("toolCall") or {}).get("functionCalls") or []
        names = {call.get("name") for call in calls if isinstance(call, dict)}
        conflicting = {"leave_voice_room", "control_voice_output"} <= names
        for call in calls:
            if not isinstance(call, dict):
                continue
            key = call.get("id")
            if not isinstance(key, str) or not key:
                continue
            if call.get("name") in {"astrbot_recall", "astrbot_remember"}:
                previous = self._memory_calls.get(key)
                if previous is not None:
                    if previous != call:
                        self._spawn(self._tool_response(call, {"ok": False, "error": "Tool identity conflict"}))
                    elif key in self._memory_results:
                        self._spawn(self._tool_response(call, self._memory_results[key]))
                    continue
                if len(self._memory_jobs) >= 4:
                    self._spawn(self._tool_response(call, {"ok": False, "error": "Memory tools busy"}))
                    continue
                self._memory_calls[key] = call
                turn = self._turn
                tools = (None if turn is not None and turn.internal else
                         turn.tools if turn is not None else self._tools)
                task = self._spawn(self._memory_tool(call, tools, turn.context if turn is not None else self.request.context,
                                                     turn.speaker if turn is not None else self._speaker, turn))
                self._memory_jobs[key] = task
                def release(done, call_id=key):
                    if self._memory_jobs.get(call_id) is done:
                        self._memory_jobs.pop(call_id, None)
                    if done.cancelled() and call_id not in self._memory_results:
                        self._memory_results[call_id] = {"ok": False, "status": "unknown",
                                                        "error": "Memory request cancelled"}
                task.add_done_callback(release)
                continue
            if key in self._controls:
                previous = self._controls[key][2]
                if previous is not None:
                    self._spawn(self._send_control(self._controls[key][1], previous))
                continue
            ref = ToolRef(self.live_session_id, self._generation, key,
                          f"{self.live_session_id}:{self._generation}:{key}")
            if len(self._controls) >= 128:
                removable = next((old for old, entry in self._controls.items()
                                  if entry[2] is not None or old in self._cancelled_controls), None)
                if removable is None:
                    self._fail("control_capacity")
                    return
                del self._controls[removable]
                self._cancelled_controls.discard(removable)
                self._claimed_controls.pop(removable, None)
            try:
                if conflicting or (self._turn is not None and self._turn.internal):
                    raise ValueError("Conflicting or internal controls")
                if call.get("name") == "leave_voice_room":
                    if not isinstance(call.get("args"), dict) or call["args"]:
                        raise ValueError("Invalid departure arguments")
                    candidate = VoiceControlDecision(leave=True)
                elif call.get("name") == "control_voice_output":
                    volume = parse_volume_command(call.get("args"))
                    if volume is None:
                        raise ValueError("No volume")
                    candidate = VoiceControlDecision(volume=volume)
                else:
                    raise ValueError("Unknown tool")
            except (ValueError, TypeError):
                rejected = ControlResolution("rejected", reason="invalid_or_conflicting_control")
                self._controls[key] = (ref, call, rejected)
                self._spawn(self._send_control(call, rejected))
                continue
            self._controls[key] = (ref, call, None)
            self._publish(LiveEvent("control_proposed", self.request.session,
                                   user_key=self._speaker, tool_ref=ref, candidate=candidate))
        server = msg.get("serverContent") or {}
        if not server:
            return
        input_text = (server.get("inputTranscription") or {}).get("text") or ""
        output_text = (server.get("outputTranscription") or {}).get("text") or ""
        model_turn = server.get("modelTurn") or {}
        turn = self._turn
        if turn is not None and turn.provider_done.is_set():
            if input_text and turn.input_ready.is_set():
                self._last = turn
                turn = self._turn = None
            elif not model_turn and not output_text and input_text and turn.state in {"pending", "allowed"}:
                pass  # 可证明属于待许可已完成轮次的晚转录。
            elif model_turn or output_text:
                if turn.state in {"allowed", "rejected"}:
                    self._finalize_input(turn)
                if turn.state == "pending":
                    turn.state = "superseded"
                    self._publish(LiveEvent("turn_finished", self.request.session, turn.ref,
                        turn.user_text, turn.speaker, reason="superseded"))
                self._last = turn
                turn = self._turn = None
        if turn is None and (model_turn or output_text or input_text):
            turn = self._new_turn()
        if turn is None:
            return
        if input_text and not turn.input_ready.is_set():
            turn.user_text += input_text
            if turn.state == "pending":
                turn.ref = ProposalRef(self.live_session_id, turn.ref.turn_id, turn.ref.revision + 1)
                self._propose(turn, "updated")
        turn.text += output_text
        for part in model_turn.get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            data = inline.get("data")
            if not data or turn.state not in {"pending", "allowed"}:
                continue
            try:
                pcm = base64.b64decode(data, validate=True)
                mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
                rate = int(mime.split("rate=")[1].split(";")[0]) if "rate=" in mime else 24000
                if len(pcm) % 2 or not 8000 <= rate <= 96000:
                    raise ValueError("PCM")
            except (ValueError, TypeError):
                self._fail("invalid_provider_audio")
                return
            if turn.buffered_bytes + len(pcm) > self._limits.deferred_audio_bytes:
                turn.state = "failed"
                turn.provider_done.set()
                self._fail("audio_capacity")
                return
            for offset in range(0, len(pcm), 65536):
                chunk = pcm[offset:offset + 65536]
                if turn.audio.full():
                    self._fail("audio_capacity")
                    return
                turn.audio.put_nowait((chunk, rate))
                turn.buffered_bytes += len(chunk)
        if server.get("interrupted"):
            turn.state = "interrupted"
            turn.provider_done.set()
            if turn.job is not None:
                self._spawn(turn.job.aclose())
            self._publish(LiveEvent("turn_finished", self.request.session, turn.ref,
                turn.user_text, turn.speaker, action_id=turn.action_id, reason="interrupted"))
        elif server.get("turnComplete"):
            logger.info("event=dialogue.live.generated input_chars=%s output_chars=%s state=%s",
                        len(turn.user_text), len(turn.text), turn.state)
            turn.provider_done.set()
            if turn.state == "pending":
                turn.deadline = min(turn.deadline, time.monotonic()
                                    + self._limits.late_transcription_seconds)
                if turn.timer is not None:
                    turn.timer.cancel()
                turn.timer = self._spawn(self._expire(turn))
                self._propose(turn, "updated")
            elif turn.state in {"allowed", "rejected"}:
                if turn.internal:
                    self._finalize_input(turn)
                else:
                    self._schedule_input_finalization(turn)
            self._speaker = ""
            self._speakers.clear()

    def transport_lost(self) -> None:
        if not self._closed.is_set():
            self._fail("transport_lost")

    def snapshot(self) -> LiveSnapshot:
        turn = self._turn or self._last
        return LiveSnapshot(self.live_session_id, self.request.session, self._state,
                            turn.ref if turn else None, turn.action_id if turn else "", self._reason)

    async def events(self):
        if self._claimed:
            raise RuntimeError("Live events allow one consumer")
        self._claimed = True
        while True:
            if not self._events.empty():
                yield self._events.get_nowait()
            elif self._closed.is_set():
                return
            else:
                getter = asyncio.create_task(self._events.get())
                closed = asyncio.create_task(self._closed.wait())
                try:
                    await asyncio.wait({getter, closed}, return_when=asyncio.FIRST_COMPLETED)
                    if getter.done():
                        yield getter.result()
                finally:
                    for task in (getter, closed):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(getter, closed, return_exceptions=True)

    async def aclose(self) -> None:
        self._closed.set()
        self._state = "closed" if self._state != "failed" else "failed"
        for turn in self._turns.values():
            if turn.state in {"pending", "allowed"}:
                turn.state = "cancelled"
            turn.provider_done.set()
            turn.input_ready.set()
            turn.authorization_ready.set()
            if turn.job is not None:
                await turn.job.aclose()
        tasks = [task for task in tuple(self._tasks) if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._backend._task_session is self:
            self._backend._task_session = None
            await self._backend.close()
