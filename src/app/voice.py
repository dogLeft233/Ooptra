"""语音对话编排。

Live 模式（默认，gemini_live）：远端 PCM 持续灌入 Live 会话，模型音频直接推回，
不做「切句 → ASR → LLM → TTS」。

级联模式（mimo_cascade）：VAD 断句后走 ASR/LLM/TTS，作为兜底。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from app.live import LiveFlow
from app.recognition import RecognitionInputs
from behavior import (
    create_barge_in_gate,
    create_behavior,
    create_reply_scheduler,
)
from behavior.models import (
    VOICE_CONTROL_MIN_MS,
    ScheduledReply,
    SpeechActivity,
    VolumeCommand,
    resolve_volume_command,
)
from channels import create_audio_ingress, create_voice_binding
from channels import create_output_controls as VoiceOutputControls
from channels.api import Channel, Duplex, PlatformDirectory
from channels.models import AudioInputPolicy, JoinRoom, LeaveRoom, RoomTarget, VadvConfig
from channels.models import RoomOperation as VoiceOperation
from context import create_context_service
from core.audio import pcm16_rms
from dialogue import create_backend
from dialogue.api import (
    ConnectionReporter,
)
from operations.models import VoiceAgentSettings, VoiceApiSettings

logger = logging.getLogger(__name__)


def _is_live_backend(backend: Any) -> bool:
    return backend.capabilities().live


#: 抢话门限所用的能量阈值。与 ``EnergyVad`` 的默认值同源：低于这个电平的帧
#: 不计入「有人在说话」，所以一声咳嗽/键盘声不会开始累计时长。
_BARGE_IN_ENERGY = VadvConfig.energy_threshold

#: 级联模式下，整段 utterance 的平均 RMS 低于此值就判定为「不是人话」，直接丢弃、
#: 不送 ASR。取值来自真实录音分布（1305 个 1 秒窗：真说话 0.03~0.22，误触发带
#: 0.012~0.02），卡在两者之间。见 ``_on_remote_pcm`` 里的详细说明。
_MIN_UTTERANCE_RMS = 0.02

#: 一条回合在信箱里等到超过「冷却时长 + 这个秒数」就丢弃，不再回。
#: 单槽信箱已经保证只留最新的一条，所以「过期」只可能是用户说完就安静了 ——
#: 此时回它反而是自言自语。留 3 秒是因为 ASR/LLM 本身也要一点时间。
_STALE_ROUND_SEC = 3.0

#: 诊断开关：把浏览器推上来的远端 PCM **原样落盘**，用于回答「Gemini 到底收到了什么」。
#: 设了 ``OOPTRA_PCM_DUMP=<前缀>`` 才生效，产出 ``<前缀>.pcm``（裸 16bit 单声道）
#: 与 ``<前缀>.tsv``（每块一行：单调时钟 / uid / 字节数 / 采样率）。
#: 两者一比就能看出**丢没丢音频**：墙钟走了 60 秒，而 .pcm 只有 20 秒的量 = 丢了 2/3。
#: 默认不设 —— 落盘在音频热路径上，别在正常运行时开。
_pcm_dump: dict[str, Any] = {"prefix": None, "fh": None, "ts": None}


def _dump_remote_pcm(uid: str, pcm: bytes, sample_rate: int) -> None:
    prefix = os.environ.get("OOPTRA_PCM_DUMP")
    if not prefix:
        return
    if _pcm_dump["prefix"] != prefix:
        for key in ("fh", "ts"):
            if _pcm_dump[key] is not None:
                _pcm_dump[key].close()
        parent = os.path.dirname(prefix)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 句柄要跨音频帧一直开着（每帧都 open/close 会把热路径拖垮），
        # 所以这里不用 with；进程退出时由操作系统回收。
        _pcm_dump["fh"] = open(prefix + ".pcm", "ab")  # noqa: SIM115
        _pcm_dump["ts"] = open(prefix + ".tsv", "a", encoding="utf-8")  # noqa: SIM115
        _pcm_dump["prefix"] = prefix
    _pcm_dump["fh"].write(pcm)
    _pcm_dump["fh"].flush()
    _pcm_dump["ts"].write(f"{time.monotonic():.3f}\t{uid}\t{len(pcm)}\t{sample_rate}\n")
    _pcm_dump["ts"].flush()

# 改了这些字段就必须重建记忆 / VAD / 已建立的模型会话，否则旧值继续生效
_MEMORY_FIELDS = {"memory_path", "memory_max_turns"}
_VAD_FIELDS = {"sample_rate_in", "silence_ms", "max_utterance_ms"}
_SESSION_FIELDS = {
    "voice_volume_enabled",
    "voice_leave_enabled",
    "persona",
    "proxy",
    "gemini_api_key",
    "gemini_base_url",
    "gemini_model",
    "gemini_voice",
    "openai_api_key",
    "openai_base_url",
    "openai_realtime_model",
    "openai_voice",
    "mimo_api_key",
    "mimo_base_url",
    "mimo_asr_model",
    "mimo_llm_model",
    "mimo_tts_model",
    "mimo_tts_voice",
    "dashscope_api_key",
    "dashscope_ws_url",
    "aliyun_asr_model",
    "aliyun_tts_model",
    "aliyun_tts_voice",
    "deepseek_api_key",
    "deepseek_base_url",
    "deepseek_model",
}


class VoiceCoordinator:
    def __init__(
        self,
        settings: VoiceAgentSettings,
        api_settings: VoiceApiSettings,
        *,
        bot: Any = None,
        default_target: Callable[[], tuple[str, str]] = lambda:("", ""),
        departure_text: Callable[[str], str] | None = None,
        subtitle_target: Callable[[], RoomTarget | None] = lambda:None,
    ) -> None:
        from behavior import departure_template
        self._default_target = default_target
        self._subtitle_target = subtitle_target
        self._departure_text = departure_text or (lambda area:departure_template(area=area))
        self.settings = settings
        self.api_settings = api_settings
        self._binding = create_voice_binding(bot, on_audio=self._on_remote_pcm,
                                              controls=lambda:self.output_controls,
                                              on_gap=lambda _uid:self._input_gap())
        self.context_service = create_context_service(
            settings.memory_path, max_turns=settings.memory_max_turns,
            outbox_path=Path(settings.memory_path).with_name("astrbot_voice_outbox.json"))
        self.memory = self.context_service.history
        self.memory.set_persona(settings.persona)
        self.backend = create_backend(settings)
        self.context_bridge = self.context_service.bridge
        self.live_mode = _is_live_backend(self.backend)
        self.output_controls = VoiceOutputControls(settings, lambda: self.duplex, lambda: self._joined)
        self.ingress = create_audio_ingress(self._input_policy(), resolve_speaker=self._binding.resolve_speaker)
        self._task: asyncio.Task | None = None
        self._live_flow: LiveFlow | None = None
        self._close_task: asyncio.Task | None = None
        self._context_closed = False
        self._busy = asyncio.Lock()
        self._stream_reply_task: asyncio.Task | None = None
        self._speaking = False
        # Live 模式的抢话闸门：一次「模型出声」最多只打断一次。见 _on_remote_pcm。
        self._barge_gate = create_barge_in_gate()
        #: uid -> 本回合内该用户**连续说话**的累计毫秒数（说话一断就清零）。
        #: 累计到 settings.barge_in_hold_ms 才认作真的抢话，见 _barge_in_reached。
        self._init_operations()
        self.last_reply = ""
        self.last_text_outcome = None
        self.last_text_reason = "disabled"
        self.last_user_text = ""
        self.turns = 0
        self.behavior = create_behavior()

        self._init_throttle()
        from app.observation import FlowObservations
        self.observations = FlowObservations()

    @property
    def platform_bound(self):
        return self._binding.connected

    @property
    def subtitle_target(self) -> RoomTarget | None:
        """只有装配明确配置的文字目标；不能用当前语音房作默认字幕落点。"""
        return self._subtitle_target()

    @property
    def channels(self) -> Channel:
        return self._binding.channel

    @property
    def directory(self) -> PlatformDirectory:
        return self._binding.directory

    @property
    def duplex(self) -> Duplex | None:
        return self._binding.output

    @duplex.setter
    def duplex(self, value):
        self._binding.output = value

    @property
    def _joined(self) -> bool:
        view = self.channels.snapshot()
        return view.confirmed

    @property
    def _area(self) -> str:
        session = self.channels.snapshot().session
        return session.room_target.area if session else self.settings.area

    @property
    def _channel(self) -> str:
        session = self.channels.snapshot().session
        return session.room_target.channel if session else self.settings.channel

    def _init_operations(self) -> None:
        self._operation_lock = asyncio.Lock()
        self._operation_callback: Callable[[VoiceOperation], Awaitable[None]] | None = None
        self._operation_epoch = 0
        self._join_source = ""
        self._visit_id = ""
        self._retiring_visit_id = ""
        self._connection_ready = True
        self._voice_suspended = False
        self._reply_generating = False
        self._reply_tasks: set[asyncio.Task] = set()
        self.recognition_inputs = RecognitionInputs(self)
        self._announcement_task: asyncio.Task | None = None
        self._announcement_cancel_requests: set[asyncio.Task] = set()
        self._announcement_active = False
        self._announcement_collecting = False
        self._voice_control_task: asyncio.Task | None = None

    def set_operation_callback(self, handler: Callable[[VoiceOperation], Awaitable[None]] | None) -> None:
        self._operation_callback = handler

    @property
    def operation_epoch(self) -> int:
        """Internal coordination guard; never included in user-facing status."""
        return self._operation_epoch

    async def _emit_operation(self, event: VoiceOperation) -> None:
        handler = self._operation_callback
        if handler is not None:
            try:
                await handler(event)
            except Exception:
                logger.exception("voice operation callback failed")

    def is_auto_visit_current(self, visit_id: str) -> bool:
        return bool(visit_id and self._joined and self._join_source == "auto" and self._visit_id == visit_id)

    def set_connection_ready(self, ready: bool) -> None:
        self._connection_ready = bool(ready)

    def request_voice_leave(self, *, action_id: str | None = None, expected_session=None) -> bool:
        """受理独立离场任务；离场语排空后执行同一 action_id，终态交行为。"""
        from behavior.models import NotExecuted
        from channels.models import ActionOutcome

        session = self.channels.snapshot().session
        if expected_session is not None and expected_session != session:
            return False
        if self._voice_control_task is not None and not self._voice_control_task.done():
            return action_id is None or action_id == getattr(self, '_voice_leave_action_id', '')
        if (not self.settings.voice_leave_enabled or not self.settings.enabled
                or not self._joined or not self._connection_ready or self._voice_suspended
                or self._announcement_active or self._retiring_visit_id or session is None):
            return False
        bot, channels = self._binding.identity, self.channels
        departure_id = action_id or uuid4().hex
        self._voice_leave_action_id = departure_id
        logger.info('event=voice.departure.accepted session_id=%s action_id=%s', session.session_id, departure_id)

        def current():
            return (self._binding.identity == bot and self.channels is channels
                    and channels.snapshot().session == session)

        async def depart() -> None:
            reason = 'not_executed'
            try:
                try:
                    await self.stop_current_reply()
                    if not current():
                        reason = 'superseded'
                        return
                    result = await self.speak_announcement('leave', self._departure_text(self._area))
                    if not result['ok']:
                        logger.warning('event=voice.departure.farewell_failed action_id=%s', departure_id)
                except Exception:
                    logger.warning('语音退房离场语失败，仍尝试退房', exc_info=True)
                if current():
                    result = await self.leave(source='manual', action_id=departure_id,
                                              expected_session=session)
                    if not result['ok']:
                        self.last_reply = '[error] 语音指令退房失败：' + str(result.get('error'))
                        logger.error('语音指令退房失败：%s', result.get('error'))
                        await self._recover_voice_control_failure(bot)
                    reason = 'finished'
                else:
                    reason = 'superseded'
            except asyncio.CancelledError:
                reason = 'cancelled'
                raise
            except Exception as exc:
                reason = 'failed'
                self.last_reply = f'[error] 语音指令退房失败：{exc}'
                logger.exception('语音指令退房失败')
            finally:
                actual = channels.outcome(departure_id)
                self.behavior.feedback(actual if isinstance(actual, ActionOutcome) else
                                       NotExecuted(departure_id, 'departure', reason))
                if self._voice_control_task is asyncio.current_task():
                    self._voice_control_task = None
                    self._voice_leave_action_id = ''

        self._voice_control_task = asyncio.create_task(depart(), name='voice-control-leave')
        return True

    async def _recover_voice_control_failure(self, bot: Any) -> None:
        try:
            async with self._operation_lock:
                if (not self._joined or self._binding.identity != bot or not self._connection_ready
                        or self._voice_suspended):
                    return
                self._rearm_barge_in()
                if self.duplex is not None:
                    await asyncio.wait_for(self.duplex.enable_listen(True), 1.0)
                if self.live_mode:
                    await self._wire_live()
                else:
                    await self._start_round_worker()
        except Exception as exc:
            self.last_reply += f"；接听恢复失败：{exc}"
            logger.exception("语音退房失败后的接听恢复失败")

    async def _cancel_voice_control(self) -> None:
        task = self._voice_control_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def begin_auto_retirement(self, expected_visit_id: str) -> bool:
        if not self.is_auto_visit_current(expected_visit_id):
            return False
        self._retiring_visit_id = expected_visit_id
        self.reply_scheduler.reset()
        return True

    async def _cancel_announcement(self) -> None:
        task = self._announcement_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            self._request_announcement_cancel(task)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await self._wait_announcement_cleanup(task)
                if not task.cancelled():
                    raise

    def _request_announcement_cancel(self, task: asyncio.Task) -> None:
        # Both an operation and the caller may cancel the same expression.
        # A second Task.cancel() would interrupt its asynchronous finally.
        if not task.done() and task not in self._announcement_cancel_requests:
            self._announcement_cancel_requests.add(task)
            task.cancel()

    async def _wait_announcement_cleanup(self, task: asyncio.Task) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Repeated cancellation of this waiter must not be forwarded
                # into the child's bounded model/audio cleanup.
                continue
            except Exception:
                break

    def _init_throttle(self) -> None:
        self.reply_scheduler = create_reply_scheduler()
        self._round_task: asyncio.Task | None = None
        self._barged_in = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    def bind_bot(self, bot: Any) -> None:
        self._binding.bind(bot)

    def subscribe_presence(self, handler):
        self._binding.subscribe_presence(handler)

    async def _drain_retired_channels(self) -> str | None:
        return await self._binding.drain_retired()

    def bind_duplex(self, duplex: Duplex) -> None:
        self._binding.attach(duplex)

    async def start(self) -> None:
        if self._close_task is not None:
            await asyncio.shield(self._close_task)
            self._close_task = None
        if self._context_closed:
            self.context_service = create_context_service(self.settings.memory_path,
                max_turns=self.settings.memory_max_turns,
                outbox_path=Path(self.settings.memory_path).with_name("astrbot_voice_outbox.json"))
            self.memory, self.context_bridge = self.context_service.history, self.context_service.bridge
            self.memory.set_persona(self.settings.persona)
            self._context_closed = False
        if not self.settings.enabled:
            logger.info("voice agent disabled")
            return
        self.memory.set_persona(self.settings.persona)
        if self.duplex is not None:
            await self.duplex.start()
            await self.duplex.enable_listen(True)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop(), name="voice-agent-loop")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        result = await self.leave(source="system")
        if not result.get("ok"):
            raise RuntimeError("房间收尾未确认，停止不完整")
        await self._binding.close()
        await self._close_live_flow()
        await self.backend.aclose()

    async def close(self) -> None:
        """终结应用资源；普通语音停用只调用stop，保留管理端记忆查询。"""
        async def finish():
            await self.stop()
            await self.context_service.close()
            self._context_closed = True
        if (self._close_task is None or (self._close_task.done() and
                (self._close_task.cancelled() or self._close_task.exception() is not None))):
            self._close_task = asyncio.create_task(finish(), name="app-voice-close")
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            self._close_task.exception()
            raise asyncio.CancelledError
        self._close_task.result()

    async def _run_loop(self) -> None:
        """会话保活：已进房但 Live 会话掉了就自动重连。

        Live 会话可能因为代理抖动/长时间空闲被服务端关掉；没有这个监督时，
        agent 会一直以为自己还在说话，实际音频早已进黑洞。
        """
        while True:
            await asyncio.sleep(15)
            if not (self._joined and self.live_mode and self._connection_ready and not self._voice_suspended):
                continue
            backend = self.backend
            active = getattr(backend, "session_active", None)
            if active is None or active:
                continue
            logger.warning("Live 会话已断开，正在重连…")
            try:
                await self._wire_live()
            except Exception as exc:
                logger.warning("语音会话重连失败：%s", exc)

    def _input_policy(self):
        minimum = self.settings.min_utterance_ms
        if self.settings.voice_leave_enabled or self.settings.voice_volume_enabled:
            minimum = min(minimum, VOICE_CONTROL_MIN_MS)
        return AudioInputPolicy(VadvConfig(sample_rate=self.settings.sample_rate_in,
            silence_ms=self.settings.silence_ms, max_utterance_ms=self.settings.max_utterance_ms),
            tuple(self.settings.listen_only_uids), minimum, _MIN_UTTERANCE_RMS)

    async def refresh(self, changed: list[str]) -> list[str]:
        # 思考强度由新生成任务复制；不取消当前回复、问候或控制任务。
        if changed == ["deepseek_reasoning_effort"]:
            return ["思考强度已更新，从下一次生成回复生效"]
        # 音量只改播放增益；不取消播报，也不等待进退房或重建模型会话。
        if changed == ["output_volume"]:
            return [await self._apply_output_volume()]
        await self._cancel_voice_control()
        await self._cancel_announcement()
        async with self._operation_lock:
            return await self._refresh_locked(changed)

    async def _refresh_locked(self, changed: list[str]) -> list[str]:
        """配置就地更新后，把派生对象与会话同步到新值。

        返回给用户看的提示（哪些东西被重建了）。``changed`` 是字段名列表。
        """
        if not changed:
            return []
        changed_set = set(changed)
        notes: list[str] = []

        if "output_volume" in changed_set:
            notes.append(await self._apply_output_volume())

        if _MEMORY_FIELDS & changed_set:
            await self._stop_reply_locked(restart=False)
            if hasattr(self, "context_service"):
                await self.context_service.close()
            self.context_service = create_context_service(
                self.settings.memory_path, max_turns=self.settings.memory_max_turns,
                outbox_path=Path(self.settings.memory_path).with_name("astrbot_voice_outbox.json"))
            self.memory = self.context_service.history
            self.context_bridge = self.context_service.bridge
            if self._joined:
                await self.context_bridge.prepare(self._area, self._channel)
                if not self.live_mode:
                    await self._start_round_worker()
            self.memory.set_persona(self.settings.persona)
            notes.append("记忆存储已重建")

        if _VAD_FIELDS & changed_set:
            self.ingress.configure(self._input_policy())
            notes.append("断句参数已重建")

        if "persona" in changed_set:
            self.memory.set_persona(self.settings.persona)

        if "backend" in changed_set:
            self._operation_epoch += 1
            await self._close_live_flow()
            await self.backend.aclose()
            self.backend = create_backend(self.settings)
            self.live_mode = _is_live_backend(self.backend)
            # 信箱消费端必须跟着模式走：live→cascade 不拉起它，句子进了信箱
            # 就没人消费，bot 直接变哑巴；cascade→live 不收掉它，它会抱着
            # 一条永远不会有新内容的信箱空转。
            if self._joined and not self.live_mode:
                await self._start_round_worker()
            else:
                await self._stop_round_worker()
            notes.append("语音后端已切换")

        session_dirty = bool(_SESSION_FIELDS & changed_set) or "backend" in changed_set
        if not session_dirty:
            return notes

        if not (self._joined and self.live_mode):
            # 没进房时无需重建会话，下次 join 自然用新配置
            return notes
        try:
            # start_session 对活跃会话是空操作，必须先关掉旧连接和旧音频。
            self._operation_epoch += 1
            await self._close_live_flow()
            await self.backend.aclose()
            self._speaking = False
            self._rearm_barge_in()
            if self.duplex is not None:
                await self.duplex.stop_tts()
            await self._wire_live()
        except Exception as exc:
            logger.warning("按新配置重建语音会话失败：%s", exc)
            return [*notes, f"语音会话重建失败：{exc}"]
        notes.append("语音会话已按新配置重建")
        return notes

    # ------------------------------------------------------------------
    # 进退房
    # ------------------------------------------------------------------

    async def _apply_output_volume(self) -> str:
        await self.output_controls.sync_volume(unmute=True)
        return f"语音音量已设为 {self.settings.output_volume}%"

    @property
    def output_muted(self) -> bool:
        return self.output_controls.muted

    def set_output_volume_saver(self, saver) -> None:
        self.output_controls.set_saver(saver)

    async def set_output_controls(self, *, volume: int | None = None, muted: bool | None = None) -> dict[str, Any]:
        return await self.output_controls.set(volume=volume, muted=muted)

    async def apply_voice_volume(self, raw: Any) -> dict[str, Any]:
        from behavior.models import parse_volume_command

        if (not self.settings.enabled or not self.settings.voice_volume_enabled or not self._joined
                or not self._connection_ready or self._voice_suspended or self._announcement_active
                or self._retiring_visit_id):
            return {"ok": False, "error": "当前不能执行语音音量控制"}
        try:
            command = parse_volume_command(raw)
            if command is None:
                raise ValueError("缺少音量控制指令")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return await self._apply_volume_command(command)

    async def _apply_volume_command(self, command: VolumeCommand) -> dict[str, Any]:
        result = await self.output_controls.adjust(
            lambda current: resolve_volume_command(command, current))
        self.last_reply = result.get("message") or "语音输出调整失败：" + result.get("error", "")
        logger.info("语音输出控制：%s → %s", command.action, self.last_reply)
        return result

    def _resolve_target(self, area: str, channel: str) -> tuple[str, str]:
        area = (area or self._area or "").strip()
        channel = (channel or self._channel or "").strip()
        if not area or not channel:
            default_area, default_channel = self._default_target()
            area, channel = area or default_area, channel or default_channel
        return area, channel

    async def join(self, area: str = "", channel: str = "", *, source: str = "manual", visit_id: str = "",
                   expected_operation_epoch: int | None = None) -> dict[str, Any]:
        await self._cancel_voice_control()
        if source != "auto":
            await self._cancel_announcement()
        events = []
        try:
            async with self._operation_lock:
                if expected_operation_epoch is not None and expected_operation_epoch != self._operation_epoch:
                    raise RuntimeError("voice operation superseded by manual control")
                if source == "auto" and self._joined:
                    raise RuntimeError("voice room occupied; automatic join cannot replace it")
                if self._joined:
                    old = VoiceOperation("leave", "system", self._area, self._channel, self._visit_id)
                    result = await self._leave_locked()
                    if not result["ok"]:
                        return result
                    self._join_source = ""
                    self._visit_id = ""
                    events.append(old)
                self._operation_epoch += 1
                result = await self._join_locked(area, channel)
                self._join_source = source
                self._visit_id = visit_id if source == "auto" else ""
                events.append(VoiceOperation("join", source, self._area, self._channel, self._visit_id))
        finally:
            for event in events:
                await self._emit_operation(event)
        return result

    async def _join_locked(self, area: str = "", channel: str = "") -> dict[str, Any]:
        area, channel = self._resolve_target(area, channel)
        if not area or not channel:
            raise RuntimeError("缺少 area/channel，请在配置或调用参数中指定")
        duplex = await self._binding.prepare_output()

        channels = self.channels
        result = await channels.execute(JoinRoom(
            uuid4().hex, RoomTarget(area, channel), channels.snapshot().revision))
        if channels is not self.channels:
            if result.session is not None:
                await channels.execute(LeaveRoom(uuid4().hex, result.session, "system"))
            raise RuntimeError("voice operation superseded by bot binding")
        if result.status != "completed":
            raise RuntimeError(f"voice join failed: {result.reason}")
        assert result.session is not None
        self.behavior.start_session(result.session)
        from context.models import Observation
        self.context_service.observe(Observation("session:"+result.session.session_id,
            result.session, result.session.session_id, 0, "session", complete=False))
        self.ingress.reset()
        self.output_controls.reset_mute()
        self._voice_suspended = False
        self._retiring_visit_id = ""
        self._speaking = False
        self._rearm_barge_in()
        try:
            await self.context_bridge.prepare(area, channel)
            await duplex.enable_listen(True)

            # 级联：拉起单槽信箱的消费端。Live 走服务端 VAD，不需要它。
            if not self.live_mode:
                await self._start_round_worker()

            # Live：建立双向语音会话，并把模型出声接到推流。
            if self.live_mode:
                await self._wire_live()
        except (Exception, asyncio.CancelledError):
            # Agora 已进房，后续失败或取消也必须回收房间、模型与 worker。
            await self._leave_locked()
            raise

        payload = {
            "ok": True,
            "area": area,
            "channel": channel,
            "rtc_channel": result.rtc_channel,
            "mode": "live" if self.live_mode else "cascade",
            "model_connection": self.backend.connection_status() if isinstance(self.backend, ConnectionReporter) else None,
            "conversation_window_seconds": self.settings.conversation_window_seconds if self.settings.conversation_window_enabled else 0,
        }
        logger.info("voice agent joined %s/%s mode=%s", area, channel, payload["mode"])
        return payload

    async def _close_live_flow(self):
        flow = self._live_flow
        if flow is not None:
            await flow.close()
        if self._live_flow is flow:
            self._live_flow = None

    async def _wire_live(self) -> None:
        from app.live import LiveFlow
        from behavior.models import ReplySettings

        previous = getattr(self, "_live_flow", None)
        if previous is not None:
            await previous.close()
        session = self.channels.snapshot().session
        if session is None or self.duplex is None:
            return
        epoch, source = self._operation_epoch, self.context_bridge.session_id
        def current():
            return (epoch == self._operation_epoch and session == self.channels.snapshot().session
                    and source == self.context_bridge.session_id)
        def settings():
            return ReplySettings(probability=self.settings.reply_probability_percent,
                keywords=tuple(self.settings.force_reply_keywords),
                window_seconds=(self.settings.conversation_window_seconds
                                if self.settings.conversation_window_enabled else 0),
                voice_leave_enabled=self.settings.voice_leave_enabled,
                voice_volume_enabled=self.settings.voice_volume_enabled)
        def state(playing, user_text, reply_text, outcome):
            if current():
                self._speaking = playing
                self._reply_generating = playing
                self.last_user_text = user_text or self.last_user_text
                self.last_reply = reply_text or self.last_reply
                if outcome:
                    self.turns += 1
                    self._rearm_barge_in()
        def subtitle_state(outcome, reason):
            if current():
                self.last_text_outcome, self.last_text_reason = outcome, reason
        flow = LiveFlow(backend=self.backend, context=self.context_service, behavior=self.behavior,
            channel=self.channels, session=session, current=current, settings=settings,
            on_state=state, on_leave=self.request_voice_leave,
            output_state=lambda:self.output_controls.snapshot(), observations=self.observations,
            subtitles_enabled=lambda:self.settings.reply_text_to_channel, subtitle_target=lambda:self.subtitle_target, on_subtitle=subtitle_state)
        self._live_flow = flow
        await flow.start()

    async def _start_round_worker(self) -> None:
        """拉起信箱消费端。重复进房时先收掉旧的，避免两个 worker 抢同一条。"""
        await self._stop_round_worker()
        self.reply_scheduler.reset()
        self._round_task = asyncio.create_task(self._round_worker())

    async def _stop_round_worker(self) -> None:
        """退房时收掉 worker 并清空信箱 —— 上次没处理完的句子不能带到下次进房。"""
        task, self._round_task = self._round_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.reply_scheduler.reset()
        await self.recognition_inputs.reset()

    async def leave(self, *, source: str = "manual", expected_visit_id: str | None = None,
                    expected_operation_epoch: int | None = None, action_id: str | None = None,
                    expected_session=None) -> dict[str, Any]:
        if expected_operation_epoch is not None and expected_operation_epoch != self._operation_epoch:
            return {"ok": False, "error": "stale operation"}
        if expected_session is not None and self.channels.snapshot().session != expected_session:
            return {'ok': False, 'error': 'stale session'}
        # A stale controller must not cancel an announcement in the new room.
        if expected_visit_id is not None and not self.is_auto_visit_current(expected_visit_id):
            return {"ok": False, "error": "stale visit"}
        await self._cancel_voice_control()
        await self._cancel_announcement()
        async with self._operation_lock:
            if expected_operation_epoch is not None and expected_operation_epoch != self._operation_epoch:
                return {"ok": False, "error": "stale operation"}
            if expected_visit_id is not None and not self.is_auto_visit_current(expected_visit_id):
                return {"ok": False, "error": "stale visit"}
            if expected_session is not None and self.channels.snapshot().session != expected_session:
                return {'ok': False, 'error': 'stale session'}
            was_joined = self._joined
            event = VoiceOperation("leave", source, self._area, self._channel, self._visit_id)
            self._operation_epoch += 1
            result = await self._leave_locked(source=source, action_id=action_id)
            if result["ok"]:
                self._join_source = ""
                self._visit_id = ""
        if result["ok"] and was_joined:
            await self._emit_operation(event)
        return result

    async def _leave_locked(self, *, source: str = "system", action_id: str | None = None) -> dict[str, Any]:
        retired_error = await self._drain_retired_channels()
        if retired_error is not None:
            return {"ok": False, "error": retired_error}
        if source == "system":
            self._voice_suspended = True
        self._operation_epoch += 1
        await self._stop_round_worker()
        await self._cancel_reply_tasks()
        self._speaking = False
        self._reply_generating = False
        if source == "system" and self.duplex is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.duplex.enable_listen(False), 1.0)
        aclose = getattr(self.backend, "aclose", None)
        if aclose is not None:
            try:
                await self._close_live_flow()
                await asyncio.wait_for(aclose(), 3.0)
            except Exception as exc:
                return {"ok": False, "error": f"voice session close failed: {exc}"}
        session = self.channels.snapshot().session
        if session is not None:
            outcome = await self.channels.execute(LeaveRoom(action_id or uuid4().hex, session))
            if outcome.status != "completed":
                return {"ok": False, "error": f"voice leave failed: {outcome.reason}"}
        # 退房就关掉 Live 会话：否则 Gemini 会话会一直挂着（既计费又占并发），
        # 而且下次进房时 start_session 会因为旧会话状态而变成空操作。
        self.context_bridge.clear()
        self.output_controls.reset_mute()
        self._retiring_visit_id = ""
        self._reply_generating = False
        self._speaking = False
        self._rearm_barge_in()
        self.ingress.reset()
        return {"ok": True}

    def status(self) -> dict[str, Any]:
        from app.flows import subtitle_receipt
        return {
            "subtitle": subtitle_receipt(self.last_text_outcome, self.last_text_reason),
            "astrbot_context": self.context_bridge.status(),
            "voice_observation": self.observations.snapshot(),
            "enabled": self.settings.enabled,
            "model_connection": self.backend.connection_status() if isinstance(self.backend, ConnectionReporter) else None,
            "conversation_window_seconds": self.settings.conversation_window_seconds if self.settings.conversation_window_enabled else 0,
            "backend": self.settings.backend,
            "mode": "live" if self.live_mode else "cascade",
            "joined": self._joined,
            "room_state": self.channels.snapshot().state.value,
            "room_cleanup_pending": self._binding.cleanup_pending,
            "pending_output_confirmations": int(bool(self._live_flow is not None and self._live_flow.busy)),
            "join_source": getattr(self, "_join_source", ""),
            "area": self._area,
            "channel": self._channel,
            "speaking": self._speaking,
            "output_volume": self.settings.output_volume,
            "output_muted": self.output_muted,
            "turns": self.turns,
            "cascade_attempts": self.behavior.snapshot().attempts,
            "cascade_turn_limit": self.settings.cascade_turn_limit,
            "last_user_text": self.last_user_text,
            "last_reply": self.last_reply,
            "persona_len": len(self.settings.persona),
        }

    async def wait_for_reply_end(self, timeout: float = 30.0) -> bool:

        async def wait() -> bool:
            while (self._reply_generating or self._speaking or self._busy.locked()
                   or bool(self._live_flow is not None and self._live_flow.busy)
                   or bool(getattr(self.backend, "reply_active", False))):
                await asyncio.sleep(0.02)
            if self.duplex is not None:
                drain = getattr(self.duplex, "wait_tts_complete", None)
                if drain is not None:
                    return bool((await drain(timeout))["ok"])
            return True

        try:
            return await asyncio.wait_for(wait(), max(0.0, timeout))
        except asyncio.TimeoutError:
            return False

    async def stop_current_reply(self) -> None:
        await self._cancel_announcement()
        async with self._operation_lock:
            await self._stop_reply_locked(restart=True)

    async def _stop_reply_locked(self, *, restart: bool) -> None:
        self._operation_epoch += 1
        await self._stop_round_worker()
        await self._cancel_reply_tasks()
        if self.live_mode:
            await self._close_live_flow()
            await asyncio.wait_for(self.backend.aclose(), 3.0)
        if self.duplex is not None:
            await self.duplex.stop_tts()
        self._speaking = False
        self._reply_generating = False
        if self.live_mode:
            # Gemini interrupt is intentionally a no-op. A fresh session is the
            # only safe boundary after a timed-out reply; old audio stays muted.
            if restart and self._joined:
                await self._wire_live()
        elif restart and self._joined:
            await self._start_round_worker()

    async def _cancel_reply_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._reply_tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def speak_announcement(self, kind: Literal["enter", "leave"], template: str, *, expected_visit_id: str | None = None,
                                 timeout: float = 20.0) -> dict[str, Any]:
        from app.announcements import run_announcement
        from dialogue import announcement_instruction

        try:
            announcement_instruction(kind, template)
        except ValueError as exc:
            return {"ok": False, "text": "", "error": str(exc)}
        async with self._operation_lock:
            if (not self._joined or (expected_visit_id is not None
                    and not self.is_auto_visit_current(expected_visit_id))):
                return {"ok": False, "text": "", "error": "stale visit"}
            if self._announcement_task is not None and not self._announcement_task.done():
                return {"ok": False, "text": "", "error": "announcement already active"}
            epoch = self._operation_epoch

            async def execute() -> dict[str, Any]:
                try:
                    return await run_announcement(self, kind, template, expected_visit_id, epoch, timeout)
                except asyncio.CancelledError:
                    # An operation cancelling this child is a failed expression,
                    # while cancelling its caller must still propagate below.
                    return {"ok": False, "text": "", "error": "announcement cancelled"}

            task = asyncio.create_task(execute())
            self._announcement_task = task
        try:
            # Keep caller cancellation distinct from an operation cancelling the
            # child on Python 3.10 as well as newer runtimes.
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._request_announcement_cancel(task)
            await self._wait_announcement_cleanup(task)
            raise
        finally:
            if self._announcement_task is task:
                self._announcement_task = None
            self._announcement_cancel_requests.discard(task)

    # ------------------------------------------------------------------
    # 音频进出
    # ------------------------------------------------------------------

    async def _on_remote_pcm(self, uid: str, pcm: bytes, sample_rate: int) -> None:
        if (getattr(self, "_voice_control_task", None) is not None
                or getattr(self, "_announcement_active", False) or getattr(self, "_retiring_visit_id", "")
                or not getattr(self, "_connection_ready", True) or getattr(self, "_voice_suspended", False)):
            return
        if self._cascade_limit_reached() and not self.backend.capabilities().streaming_recognition:
            return
        if not self.settings.enabled or not pcm:
            return
        generation = self.ingress.generation
        self.ingress.configure(self._input_policy())
        audio = self.ingress.accept(uid, pcm, sample_rate)
        if self.ingress.generation != generation:
            self.recognition_inputs.invalidate()
        if audio is None:
            return
        uid, pcm, sample_rate = audio.user_key, audio.pcm16, audio.sample_rate
        _dump_remote_pcm(uid, pcm, sample_rate)

        if self.live_mode:
            flow = getattr(self, "_live_flow", None)
            if flow is None:
                return
            if self.settings.barge_in and (self._speaking or flow.busy) and self._barge_in_reached(uid, pcm, sample_rate):
                await flow.interrupt()
                self._speaking = False
            await flow.send_input(uid, pcm, sample_rate)
            return

        # 级联：VAD 切句
        if (self._speaking and self.settings.barge_in
                and (not self.backend.capabilities().streaming_utterance
                     or self._barge_in_reached(uid, pcm, sample_rate))):
            await self._interrupt_speaking()
        result = self.ingress.cut(audio)
        if self.backend.capabilities().streaming_recognition:
            key = await self.recognition_inputs.feed(audio, result)
            if key:
                self.reply_scheduler.offer(uid, result.pcm16, sample_rate, input_ref=key)
        elif result.status == "ready":
            self._offer_utterance(uid, result.pcm16, sample_rate)

    def _input_gap(self) -> None:
        self.ingress.reset()
        self.recognition_inputs.invalidate()

    def _offer_utterance(self, uid: str, pcm: bytes, sample_rate: int) -> None:
        self.reply_scheduler.offer(uid, pcm, sample_rate)

    def _reply_cooldown(self) -> float:
        return max(0, int(getattr(self.settings, "reply_cooldown_ms", 0) or 0)) / 1000.0

    async def _round_worker(self) -> None:
        task = asyncio.current_task()
        while self._round_task is task:
            item = await self.reply_scheduler.next(self._reply_cooldown())
            if self._round_task is not task:
                return
            await self._run_selected_reply(item)

    async def _run_selected_reply(self, item: ScheduledReply) -> None:
        self._barged_in = False
        recognition = self.recognition_inputs.take(item.input_ref) if item.input_ref else None
        try:
            if item.input_ref:
                if recognition is None:
                    return
                await self._handle_utterance(item.user_key, item.pcm, item.sample_rate, recognition=recognition)
            else:
                await self._handle_utterance(item.user_key, item.pcm, item.sample_rate)
        finally:
            if recognition is not None:
                await self.recognition_inputs.release(recognition)
            self.reply_scheduler.finish(item, cooldown=self._reply_cooldown(), interrupted=self._barged_in)

    def _rearm_barge_in(self) -> None:
        self._barge_gate.rearm()

    def _barge_in_reached(self, uid: str, pcm: bytes, sample_rate: int) -> bool:
        rate = int(sample_rate or self.settings.sample_rate_in) or 16000
        return self._barge_gate.observe(
            SpeechActivity(uid, pcm16_rms(pcm) >= _BARGE_IN_ENERGY,
                           len(pcm) / 2 / rate * 1000),
            hold_ms=int(getattr(self.settings, "barge_in_hold_ms", 0) or 0),
        )

    async def _interrupt_speaking(self) -> None:
        self._speaking = False
        # 记下来：本回合是被抢话打断的，结束时不计冷却（见行为调度器）。
        self._barged_in = True
        stream_task = getattr(self, "_stream_reply_task", None)
        if stream_task is not None and not stream_task.done():
            stream_task.cancel()
        if self.duplex is not None:
            try:
                await self.duplex.stop_tts()
            except Exception:
                logger.debug("stop tts on barge-in failed", exc_info=True)

    async def _handle_utterance(self, uid: str, pcm: bytes, sample_rate: int, *, recognition=None) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._reply_tasks.add(task)
        try:
            await self._handle_utterance_reply(uid, pcm, sample_rate, recognition=recognition)
        finally:
            if task is not None:
                self._reply_tasks.discard(task)

    def _cascade_limit_reached(self) -> bool:
        return not self.live_mode and self.behavior.exhausted(self.settings.cascade_turn_limit)

    async def _handle_utterance_reply(self, uid: str, pcm: bytes, sample_rate: int, *, recognition=None) -> None:
        from app.flows import run_cascade
        from behavior.models import ReplySettings

        session = self.channels.snapshot().session
        if session is None or not pcm or self._announcement_active:
            return
        epoch = self._operation_epoch
        source = self.context_bridge.session_id
        async with self._busy:
            if epoch != self._operation_epoch or self._announcement_active:
                return
            self.behavior.start_session(session)
            def current():
                return (epoch == self._operation_epoch and self.channels.snapshot().session == session
                        and self.context_bridge.session_id == source)
            def playing(active):
                if current():
                    if active:
                        self.turns += 1
                        self._rearm_barge_in()
                    self._speaking = active
            try:
                flow_task = asyncio.create_task(run_cascade(
                    backend=self.backend, context=self.context_service, behavior=self.behavior,
                    channel=self.channels, session=session,
                    trigger_id=recognition.trigger.trigger_id if recognition else uuid4().hex,
                    recognition=recognition,
                    user_key=f"oopz:{uid}", pcm=pcm, sample_rate=sample_rate,
                    limit=self.settings.cascade_turn_limit,
                    settings=ReplySettings(
                        probability=self.settings.reply_probability_percent,
                        keywords=tuple(self.settings.force_reply_keywords),
                        window_seconds=(self.settings.conversation_window_seconds
                                        if self.settings.conversation_window_enabled else 0),
                        min_utterance_ms=self.settings.min_utterance_ms,
                        voice_leave_enabled=self.settings.voice_leave_enabled,
                        voice_volume_enabled=self.settings.voice_volume_enabled),
                    interpret_controls=self.settings.voice_leave_enabled or self.settings.voice_volume_enabled,
                    current=current, on_playing=playing, observations=self.observations,
                    reply_text_to_channel=self.settings.reply_text_to_channel, subtitle_target=self.subtitle_target), name="app-cascade-flow")
                self._stream_reply_task = flow_task
                try:
                    result = await flow_task
                except asyncio.CancelledError:
                    if self._barged_in:
                        return
                    raise
                finally:
                    if self._stream_reply_task is flow_task:
                        self._stream_reply_task = None
                if current():
                    self.last_text_outcome, self.last_text_reason = result.text_outcome, result.text_reason
                    self.last_user_text = result.user_text or self.last_user_text
                    self.last_reply = result.reply_text or self.last_reply
                    if result.control is not None:
                        control = result.control.control
                        if control.leave:
                            self.request_voice_leave()
                        elif control.volume is not None:
                            applied = await self._apply_volume_command(control.volume)
                            if not self.output_muted:
                                await self.speak_text(applied.get("message") or "语音输出调整失败")
            except Exception:
                logger.exception("event=voice.flow.failed session_id=%s", session.session_id)

    async def speak_text(self, text: str) -> dict[str, Any]:
        if self._announcement_active:
            return {"ok": False, "error": "announcement active"}
        self._reply_generating = True
        task = asyncio.current_task()
        if task is not None:
            self._reply_tasks.add(task)
        try:
            result = await self._speak_text(text)
            if not self.live_mode or not result.get("ok"):
                self._reply_generating = False
            return result
        except BaseException:
            self._reply_generating = False
            raise
        finally:
            if task is not None:
                self._reply_tasks.discard(task)

    async def _speak_text(self, text: str) -> dict[str, Any]:
        """快捷开口：Live 走 realtimeInput.text；级联走 TTS。"""
        session = self.channels.snapshot().session
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "text required"}

        if not self._joined:
            return {"ok": False, "error": "not joined to a voice room"}
        if self.duplex is None or not self._connection_ready or self._voice_suspended:
            return {"ok": False, "error": "voice playback unavailable"}

        if self.live_mode:
            flow = getattr(self, "_live_flow", None)
            if flow is None:
                return {"ok": False, "error": "实时会话未就绪"}
            return await flow.speak(text)

        from app.flows import run_expression
        epoch = self._operation_epoch
        source = self.context_bridge.session_id
        def current():
            return (epoch == self._operation_epoch and session == self.channels.snapshot().session
                    and source == self.context_bridge.session_id)
        if self.duplex is None or self.output_muted:
            return {"ok": False, "error": "没有可播放的音频输出"}
        self._speaking = True
        try:
            result = await run_expression(backend=self.backend, context=self.context_service,
                channel=self.channels, session=session, text=text, current=current,
                reply_text_to_channel=self.settings.reply_text_to_channel, subtitle_target=self.subtitle_target)
        finally:
            if current():
                self._speaking = False
        if current():
            self.last_text_outcome, self.last_text_reason = result.text_outcome, result.text_reason
        if result.outcome is None or result.outcome.status != "completed":
            return {"ok": False, "error": result.reason or "语音播放未正常完成", "subtitle": result.subtitle}
        self.last_reply = result.reply_text
        self.turns += 1
        return {"ok": True, "chars": len(text), "pcm_bytes": result.outcome.submitted_samples * 2, "mode": "cascade", "subtitle": result.subtitle}

    async def refresh_astrbot_context(self) -> dict[str, Any]:
        async with self._operation_lock:
            if not self._joined:
                self.context_bridge.clear()
                return {"astrbot_context": self.context_bridge.status()}
            await self._stop_reply_locked(restart=False)
            await self.context_bridge.prepare(self._area, self._channel)
            if self.live_mode:
                await self._wire_live()
            else:
                await self._start_round_worker()
            return {"astrbot_context": self.context_bridge.status()}

    def update_persona(self, persona: str) -> None:
        self.settings.persona = str(persona or self.settings.persona)
        self.memory.set_persona(self.settings.persona)
