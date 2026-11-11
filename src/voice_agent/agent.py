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
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from behavior import (
    create_barge_in_gate,
    create_cascade_attempts,
    create_reply_policy,
    create_reply_scheduler,
)
from behavior.api import CascadeAttempts
from behavior.models import (
    VOICE_CONTROL_MIN_MS,
    AttemptRejected,
    ScheduledReply,
    SpeechActivity,
    VolumeCommand,
    resolve_volume_command,
)
from channels import create_duplex as VoiceDuplex
from channels import create_oopz_channel, create_oopz_directory, create_playback, create_vad
from channels import create_output_controls as VoiceOutputControls
from channels.api import (
    Channel,
    Duplex,
    OutputControls,
    PlatformDirectory,
    Playback,
    VoiceActivityDetector,
)
from channels.models import JoinRoom, LeaveRoom, RoomTarget, SessionRef, VadvConfig
from channels.models import RoomOperation as VoiceOperation
from context import create_context_bridge, create_memory, create_turn_submission
from context.api import SourceBridge, TurnSubmission
from context.models import ConfirmedTurn
from core.audio import pcm16_rms
from dialogue import create_backend
from dialogue.api import (
    ConnectionReporter,
    SessionStarter,
    StreamingUtterance,
)
from dialogue.models import GeneratedTurn, RecognizedGenerationFailed, VoiceStreamInterrupted
from operations.models import VoiceAgentSettings, VoiceApiSettings

logger = logging.getLogger(__name__)


def _is_live_backend(backend: Any) -> bool:
    return bool(getattr(backend, "session_active", None) is not None or
                getattr(backend, "name", "") in {"gemini_live", "openai_realtime"} or
                hasattr(backend, "push_audio"))


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


class VoiceAgent:
    def __init__(
        self,
        settings: VoiceAgentSettings,
        api_settings: VoiceApiSettings,
        *,
        bot: Any = None,
    ) -> None:
        self.settings = settings
        self.api_settings = api_settings
        self._bot = bot
        self.memory = create_memory(settings.memory_path, settings.memory_max_turns)
        self.memory.set_persona(settings.persona)
        self.backend = create_backend(settings, self.memory, reply_policy=create_reply_policy())
        self.context_bridge = create_context_bridge(
            outbox_path=Path(self.memory.path).with_name("astrbot_voice_outbox.json"))
        self.backend.context_bridge = self.context_bridge
        self.live_mode = _is_live_backend(self.backend)
        self.duplex: Duplex | None = None
        self.output_controls = VoiceOutputControls(settings, lambda: self.duplex, lambda: self._joined)
        #: 正在收尾的旧 duplex。bind_bot 是同步上下文，close() 只能交给事件循环；
        #: 留引用是为了有地方 await，也避免「Task exception was never retrieved」。
        self._duplex_close_tasks: set[asyncio.Task] = set()
        self._vad = self._make_vad()
        self._user_buffers: dict[str, VoiceActivityDetector] = {}
        self._task: asyncio.Task | None = None
        self._busy = asyncio.Lock()
        self._stream_reply_task: asyncio.Task | None = None
        self._speaking = False
        # Live 模式的抢话闸门：一次「模型出声」最多只打断一次。见 _on_remote_pcm。
        self._barge_gate = create_barge_in_gate()
        self._discard_live_audio = False
        self._live_output_lock = asyncio.Lock()
        #: uid -> 本回合内该用户**连续说话**的累计毫秒数（说话一断就清零）。
        #: 累计到 settings.barge_in_hold_ms 才认作真的抢话，见 _barge_in_reached。
        self._init_operations()
        self.last_reply = ""
        self.last_user_text = ""
        self.turns = 0
        self._attempt_budget = create_cascade_attempts()

        self._init_throttle()

    @property
    def channels(self) -> Channel:
        channel = getattr(self, "_channels", None)
        if channel is None:
            channel = create_oopz_channel(getattr(self, "_bot", None), lambda: self.duplex)
            self._channels = channel
        return channel

    @property
    def directory(self) -> PlatformDirectory:
        directory = getattr(self, "_directory", None)
        if directory is None:
            directory = create_oopz_directory(getattr(self, "_bot", None))
            self._directory = directory
        return directory

    def _ensure_attempt_budget(self) -> CascadeAttempts:
        if not hasattr(self, "_attempt_budget"):
            self._attempt_budget = create_cascade_attempts()
        return self._attempt_budget

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
        self._completion_tasks: set[asyncio.Task] = set()
        self._playbacks: set[Playback] = set()
        self._playback_epoch = 0
        self._announcement_task: asyncio.Task | None = None
        self._announcement_cancel_requests: set[asyncio.Task] = set()
        self._announcement_active = False
        self._announcement_collecting = False
        self._announcement_done = asyncio.Event()
        self._announcement_audio = 0
        self._announcement_text = ""
        self._announcement_error = ""
        self._voice_control_task: asyncio.Task | None = None

    def _ensure_operations(self) -> None:
        # Existing test helpers and integrations construct an agent with __new__.
        if not hasattr(self, "_operation_lock"):
            self._init_operations()
        self._ensure_context_bridge()

    def _ensure_context_bridge(self) -> SourceBridge:
        if not hasattr(self, "context_bridge"):
            self.context_bridge = create_context_bridge()
            self.backend.context_bridge = self.context_bridge
        return self.context_bridge

    def set_operation_callback(self, handler: Callable[[VoiceOperation], Awaitable[None]] | None) -> None:
        self._ensure_operations()
        self._operation_callback = handler

    @property
    def operation_epoch(self) -> int:
        """Internal coordination guard; never included in user-facing status."""
        self._ensure_operations()
        return self._operation_epoch

    async def _emit_operation(self, event: VoiceOperation) -> None:
        handler = self._operation_callback
        if handler is not None:
            try:
                await handler(event)
            except Exception:
                logger.exception("voice operation callback failed")

    def is_auto_visit_current(self, visit_id: str) -> bool:
        self._ensure_operations()
        return bool(visit_id and self._joined and self._join_source == "auto" and self._visit_id == visit_id)

    def set_connection_ready(self, ready: bool) -> None:
        self._ensure_operations()
        self._connection_ready = bool(ready)

    def request_voice_leave(self) -> bool:
        """Schedule separately from the reader/round worker that must be closed on exit."""
        self._ensure_operations()
        if (not self.settings.voice_leave_enabled or not self.settings.enabled
                or not self._joined or not self._connection_ready or self._voice_suspended
                or self._announcement_active or self._retiring_visit_id):
            return False
        if self._voice_control_task is not None and not self._voice_control_task.done():
            return True
        bot = self._bot
        logger.info("收到语音退房意图，先播放离场语：%s/%s", self._area, self._channel)

        async def depart() -> None:
            try:
                try:
                    await self.stop_current_reply()
                    if not self._joined or self._bot is not bot:
                        return
                    import config as runtime_config
                    from behavior import (
                        effective_area,
                        parse_auto_visit_config,
                    )

                    policy = effective_area(parse_auto_visit_config(
                        getattr(runtime_config, "VOICE_AUTO_VISIT_CONFIG", None)
                    ), self._area)
                    templates = policy["leave_prompts"] or ["拜拜，我下了"]
                    result = await self.speak_announcement("leave", random.choice(templates))
                    if not result["ok"]:
                        logger.warning("语音退房离场语未完成：%s", result.get("error"))
                except Exception:
                    logger.warning("语音退房离场语失败，仍尝试退房", exc_info=True)
                if self._joined and self._bot is bot:
                    result = await self.leave(source="manual")
                    if not result["ok"]:
                        self.last_reply = "[error] 语音指令退房失败：" + str(result.get("error"))
                        logger.error("语音指令退房失败：%s", result.get("error"))
                        await self._recover_voice_control_failure(bot)
                    else:
                        logger.info("语音指令退房成功")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_reply = f"[error] 语音指令退房失败：{exc}"
                logger.exception("语音指令退房失败")
            finally:
                if self._voice_control_task is asyncio.current_task():
                    self._voice_control_task = None

        self._voice_control_task = asyncio.create_task(depart(), name="voice-control-leave")
        return True

    async def _recover_voice_control_failure(self, bot: Any) -> None:
        try:
            async with self._operation_lock:
                if (not self._joined or self._bot is not bot or not self._connection_ready
                        or self._voice_suspended):
                    return
                self._rearm_barge_in()
                if self.duplex is not None:
                    await asyncio.wait_for(self.duplex.enable_listen(True), 1.0)
                if self.live_mode:
                    await self._wire_live()
                    assert isinstance(self.backend, SessionStarter), "Live backend requires start_session"
                    await asyncio.wait_for(self.backend.start_session(), 5.0)
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
        self._ensure_operations()
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
        """换绑 Oopz bot —— **连同它的浏览器 transport 一起**。

        「重启桥接」（WebUI 手动重启、凭据变更、重连重建）会整个换掉
        ``OopzBot``，而每个 bot 都自带新的 ``Voice`` 与新的
        ``BrowserVoiceTransport``（新的 Playwright 浏览器与页面）。duplex 是
        **绑定在具体 transport 上**的资源，不换绑就等于：

          * 模型音频被推进一个**已经关掉的页面** —— 那里 ``client`` 为空，
            ``agoraPushTtsPcm`` 返回 ``{"ok": False, "error": "not joined"}``；
          * 远端 PCM 的回调注册在旧 transport 上，新页面的
            ``oopzPushRemotePcm`` 永远不上报，agent 一个字都听不到。

        现象极具迷惑性：**进房成功、日志照常出回合、房间里一句话都没有**，
        而且只有重启进程才能恢复（重启桥接会再切一次）。
        """
        if bot is not getattr(self, "_bot", None):
            old_channel = getattr(self, "_channels", None)
            new_voice = getattr(bot, "voice", None)
            new_transport = getattr(new_voice, "backend", None)
            if old_channel is not None and self._bot_backend() is new_transport:
                # 不同 SDK 包装可能共用浏览器；异步入口必须先退役旧会话。
                retired = getattr(self, "_retired_channels", [])
                retired.append(old_channel)
                self._retired_channels = retired
            self._channels = None
            self._directory = None
        self._bot = bot
        self._drop_stale_duplex()

    async def _drain_retired_channels(self) -> str | None:
        retired = getattr(self, "_retired_channels", [])
        while retired:
            channel = retired[0]
            session = channel.snapshot().session
            if session is not None:
                result = await channel.execute(LeaveRoom(uuid4().hex, session, "system"))
                if result.status != "completed":
                    return "旧传输尚未完成收尾，不能复用"
            retired.pop(0)
        return None

    def _bot_backend(self) -> Any:
        """当前 bot 的语音浏览器 transport（没有就返回 None）。"""
        voice = getattr(self._bot, "voice", None)
        return getattr(voice, "backend", None) if voice is not None else None

    def _drop_stale_duplex(self) -> None:
        """duplex 绑的已不是当前 bot 的 backend 时，就地作废它。

        下一次 ``join()`` 会按新的 backend 重建。这里不 await ``close()``
        （同步上下文），交给事件循环收尾：旧 transport 多半已经随
        ``bot.stop()`` 关掉了，``close()`` 会直接返回。
        """
        duplex = self.duplex
        if duplex is None:
            return
        backend = self._bot_backend()
        if backend is None or duplex.transport is backend:
            return
        self.duplex = None
        # bot 都没了，房间自然也不在了；不复位会让 speak_text 继续往死页面推
        self._ensure_output_controls().reset_mute()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(duplex.close(), name="voice-duplex-close")
        self._duplex_close_tasks.add(task)
        task.add_done_callback(self._duplex_close_tasks.discard)

    def bind_duplex(self, duplex: Duplex) -> None:
        self.duplex = duplex
        self._ensure_output_controls().attach(duplex)
        duplex.set_remote_pcm_handler(self._on_remote_pcm)

    async def start(self) -> None:
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
        try:
            await self.leave(source="system")
        except Exception:
            logger.debug("leave on stop failed", exc_info=True)
        if self.duplex is not None:
            await self.duplex.close()
        await self.backend.aclose()

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
            start = getattr(backend, "start_session", None)
            if start is None:
                continue
            logger.warning("Live 会话已断开，正在重连…")
            try:
                await start()
            except Exception as exc:
                logger.warning("语音会话重连失败：%s", exc)

    def _make_vad(self) -> VoiceActivityDetector:
        return create_vad(
            VadvConfig(
                sample_rate=self.settings.sample_rate_in,
                silence_ms=self.settings.silence_ms,
                max_utterance_ms=self.settings.max_utterance_ms,
            )
        )

    async def refresh(self, changed: list[str]) -> list[str]:
        # 音量只改播放增益；不取消播报，也不等待进退房或重建模型会话。
        if changed == ["output_volume"]:
            return [await self._apply_output_volume()]
        self._ensure_operations()
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
            self.memory = create_memory(self.settings.memory_path, self.settings.memory_max_turns)
            self.memory.set_persona(self.settings.persona)
            self.backend.bind_history(self.memory)
            notes.append("记忆存储已重建")

        if _VAD_FIELDS & changed_set:
            self._vad = self._make_vad()
            self._user_buffers.clear()
            notes.append("断句参数已重建")

        if "persona" in changed_set:
            self.memory.set_persona(self.settings.persona)

        if "backend" in changed_set:
            self._operation_epoch += 1
            await self.backend.aclose()
            self.backend = create_backend(self.settings, self.memory, reply_policy=create_reply_policy())
            self.backend.context_bridge = self.context_bridge
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
        start = getattr(self.backend, "start_session", None)
        if start is None:
            return notes
        try:
            # start_session 对活跃会话是空操作，必须先关掉旧连接和旧音频。
            self._operation_epoch += 1
            await self.backend.aclose()
            self._speaking = False
            self._rearm_barge_in()
            if self.duplex is not None:
                await self.duplex.stop_tts()
            await self._wire_live()
            await start()
        except Exception as exc:
            logger.warning("按新配置重建语音会话失败：%s", exc)
            return [*notes, f"语音会话重建失败：{exc}"]
        notes.append("语音会话已按新配置重建")
        return notes

    # ------------------------------------------------------------------
    # 进退房
    # ------------------------------------------------------------------

    async def _apply_output_volume(self) -> str:
        await self._ensure_output_controls().sync_volume(unmute=True)
        return f"语音音量已设为 {self.settings.output_volume}%"

    def _ensure_output_controls(self) -> OutputControls:
        if not hasattr(self, "output_controls"):
            self.output_controls = VoiceOutputControls(self.settings, lambda: self.duplex, lambda: self._joined)
        return self.output_controls

    @property
    def output_muted(self) -> bool:
        return self._ensure_output_controls().muted

    def set_output_volume_saver(self, saver) -> None:
        self._ensure_output_controls().set_saver(saver)

    async def set_output_controls(self, *, volume: int | None = None, muted: bool | None = None) -> dict[str, Any]:
        return await self._ensure_output_controls().set(volume=volume, muted=muted)

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
        result = await self._ensure_output_controls().adjust(
            lambda current: resolve_volume_command(command, current))
        self.last_reply = result.get("message") or "语音输出调整失败：" + result.get("error", "")
        logger.info("语音输出控制：%s → %s", command.action, self.last_reply)
        return result

    def _resolve_target(self, area: str, channel: str) -> tuple[str, str]:
        area = (area or self._area or "").strip()
        channel = (channel or self._channel or "").strip()
        if not area or not channel:
            try:
                import config as runtime_config

                area = area or str(runtime_config.OOPZ_CONFIG.get("default_area") or "")
                channel = channel or str(runtime_config.OOPZ_CONFIG.get("default_channel") or "")
            except Exception:
                pass
        return area, channel

    async def join(self, area: str = "", channel: str = "", *, source: str = "manual", visit_id: str = "",
                   expected_operation_epoch: int | None = None) -> dict[str, Any]:
        self._ensure_operations()
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
                area, channel = self._resolve_target(area, channel)
                if not area or not channel:
                    raise RuntimeError("缺少 area/channel，请在配置或调用参数中指定")
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
        if self._bot is None:
            raise RuntimeError("Oopz bot 尚未就绪")
        retired_error = await self._drain_retired_channels()
        if retired_error is not None:
            raise RuntimeError(retired_error)
        area, channel = self._resolve_target(area, channel)
        if not area or not channel:
            raise RuntimeError("缺少 area/channel，请在配置或调用参数中指定")
        # duplex 按 transport 身份复用：绑的还是当前 bot 的浏览器就接着用，
        # 否则整套重建（bind_bot 已在换 bot 时置空，这里是兜底复检）。
        voice = getattr(self._bot, "voice", None)
        backend = getattr(voice, "backend", None) if voice is not None else None
        if self.duplex is None or (backend is not None and self.duplex.transport is not backend):
            if voice is None:
                raise RuntimeError("bot.voice 不存在")
            old = self.duplex
            self.duplex = None
            if old is not None:
                with contextlib.suppress(Exception):
                    await old.close()
            duplex = VoiceDuplex(backend)
            self.bind_duplex(duplex)
            await duplex.start()
            await duplex.enable_listen(True)

        duplex = self.duplex
        assert duplex is not None, "Voice duplex must be bound before joining"

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
        self._ensure_attempt_budget().start_session(result.session)
        self._ensure_output_controls().reset_mute()
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
                start = getattr(self.backend, "start_session", None)
                if start is not None:
                    await start()
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

    async def _push_room_pcm(self, session: SessionRef | None, pcm: bytes, rate: int, *, finish: bool = False) -> dict[str, Any]:
        if session is None:
            return {"ok": False, "error": "stale_session"}
        push = asyncio.create_task(self.channels.push_audio(session, pcm, rate, finish=finish))
        try:
            result = await asyncio.shield(push)
        except asyncio.CancelledError:
            while not push.done():
                try:
                    await asyncio.shield(push)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not push.cancelled():
                push.exception()
            raise
        return {"ok": result.ok, "error": result.reason}

    def _start_playback(self, session: SessionRef | None, output: Duplex) -> Playback:
        playback = create_playback(self.channels, session, output)
        self._playbacks.add(playback)
        return playback

    async def _wire_live(self) -> None:
        self._ensure_operations()
        duplex = self.duplex
        if duplex is None:
            return
        self._discard_live_audio = False
        epoch = self._operation_epoch
        channels = self.channels
        session = channels.snapshot().session
        source_session = self._ensure_context_bridge().session_id

        def current() -> bool:
            return (epoch == self._operation_epoch and channels is self.channels
                    and channels.snapshot().session == session
                    and self.context_bridge.session_id == source_session)

        # 每回合只报一次推流失败：失败通常整回合都失败，逐分片刷屏反而盖住线索
        push_failed = False
        turn_failed = False
        turn_interrupted = False
        audio_bytes = 0
        audio_rate = self.settings.sample_rate_out
        playback = create_playback(channels, session, duplex)

        async def on_audio(pcm: bytes, rate: int) -> None:
            if not current():
                return
            if self._voice_control_task is not None and not self._announcement_collecting:
                return
            # 收到分片只说明「这一回合正在出声」，**不代表回合结束**：
            # 以前在这里 turns += 1，一次回复实测 13 个分片就虚增 13 轮；
            # 而 _speaking 置 True 后没有任何地方回落，导致下面 _on_remote_pcm
            # 的抢话分支被每个分片各触发一次。回合边界一律由 on_turn_end 负责。
            nonlocal push_failed, turn_failed, audio_bytes, audio_rate
            self._playbacks.add(playback)
            async with self._live_output_lock:
                if not current() or self._discard_live_audio:
                    return
                self._speaking = True
                pushed = await playback.push(pcm, rate)
                result = {"ok": pushed.ok, "error": pushed.reason}
            # 推流失败必须留痕。这里曾经整条链路静默失败（页面已不是当前会话，
            # 返回值 {ok: False, error: "not joined"} 被丢弃），日志里只有
            # 「模型出了回合」，排查时完全看不出音频根本没进房间。
            if isinstance(result, dict) and result.get("ok") is False:
                turn_failed = True
                if self._announcement_active and self._announcement_collecting:
                    self._announcement_error = result.get("error") or "audio push failed"
                if not push_failed:
                    push_failed = True
                    logger.warning(
                        "模型音频推流失败，房间听不到：%s",
                        result.get("error") or result,
                    )
            else:
                audio_bytes += len(pcm)
                audio_rate = rate
                push_failed = False
                if self._announcement_active and self._announcement_collecting and pcm:
                    self._announcement_audio += len(pcm)

        async def on_text(text: str) -> None:
            if not current():
                return
            self.last_reply = text
            if self._announcement_active and self._announcement_collecting:
                self._announcement_text = text

        async def on_turn_end(interrupted: bool) -> None:
            """一个回合结束（说完了，或被抢话打断）。"""
            nonlocal push_failed, turn_interrupted
            if not current():
                return
            turn_interrupted = turn_interrupted or interrupted or self._discard_live_audio
            self._reply_generating = False
            self._speaking = False
            self.turns += 1
            # 下一回合允许再打断一次（顺带清掉本回合累计的连续说话时长）
            self._rearm_barge_in()
            # 失败计数按回合清零：下一回合若还坏，还能再报一条
            push_failed = False
            if interrupted:
                self._playback_epoch += 1
            if interrupted and self.duplex is not None:
                # 服务端自己判定用户抢话：模型已经停口，但本地排期的分片还在播，
                # 必须就地清掉，否则「模型不说了、喇叭还在说」直到缓冲播完。
                with contextlib.suppress(Exception):
                    await self.duplex.stop_tts()

        async def on_generated(result: GeneratedTurn) -> None:
            nonlocal turn_failed, turn_interrupted, audio_bytes, playback
            if not current():
                return
            failed, emitted, discarded = turn_failed, audio_bytes, turn_interrupted
            turn_failed, audio_bytes, turn_interrupted = False, 0, False
            completed_playback = playback
            playback = create_playback(channels, session, duplex)
            finished_rate = audio_rate
            if result.interrupted or discarded or self._discard_live_audio:
                completed_playback.interrupt()

            submission = create_turn_submission(self.memory, self.context_bridge,
                                                speaker="api" if result.internal else result.user_key)
            input_turn = ConfirmedTurn(result.user_text, "", "interrupted",
                user_key=(result.user_key if result.user_key.startswith("oopz:")
                          else f"oopz:{result.user_key}"),
                channel_key=f"{session.room_target.area}/{session.room_target.channel}" if session else "")

            async def complete() -> None:
                recorded = False
                try:
                    evidence = await completed_playback.finish(finished_rate)
                    confirmation: Literal["completed", "silent", "interrupted", "failed", "unknown"] = evidence.status
                    if confirmation == "completed" and not result.text:
                        confirmation = "unknown"
                    if result.internal and self._announcement_active:
                        from dialogue import clean_announcement
                        self._announcement_text = clean_announcement(result.text)
                        if evidence.status != "completed":
                            self._announcement_error = "announcement playback " + confirmation
                        self._announcement_done.set()
                        return
                    if current():
                        await self._record_output(
                            "" if result.internal else result.user_text,
                            result.text if confirmation == "completed" else "",
                            "api" if result.internal else result.user_key,
                            session, confirmation, submission,
                        )
                        recorded = True
                finally:
                    try:
                        if not recorded and not result.internal and result.user_text and session is not None:
                            await submission.record(input_turn)
                    finally:
                        self._playbacks.discard(completed_playback)

            if not emitted or result.interrupted or discarded or failed:
                await complete()
                return
            # 播放确认由应用任务拥有，不占用模型接收循环。满载时明确降级，
            # 不把缺少确认的草稿写成回复；停止/切房通过 _reply_tasks 统一取消回收。
            if len(self._completion_tasks) >= 8:
                self._playbacks.discard(completed_playback)
                logger.warning("event=voice.history.capacity_rejected session_id=%s", session.session_id if session else "")
                return
            task = asyncio.create_task(complete(), name="voice-output-confirmation")
            self._reply_tasks.add(task)
            self._completion_tasks.add(task)

            def observed(done: asyncio.Task) -> None:
                self._reply_tasks.discard(done)
                self._completion_tasks.discard(done)
                if not done.cancelled() and done.exception() is not None:
                    logger.error("event=voice.history.commit_failed session_id=%s", session.session_id if session else "",
                                 exc_info=done.exception())

            task.add_done_callback(observed)

        setter_generated = getattr(self.backend, "set_generated_out", None)
        if setter_generated is not None:
            setter_generated(on_generated)

        setter_audio = getattr(self.backend, "set_audio_out", None)
        if setter_audio:
            setter_audio(on_audio)
        setter_text = getattr(self.backend, "set_text_out", None)
        if setter_text:
            setter_text(on_text)
        setter_turn = getattr(self.backend, "set_turn_out", None)
        if setter_turn:
            setter_turn(on_turn_end)

        def on_reply(active: bool) -> None:
            if current():
                self._reply_generating = active

        setter_reply = getattr(self.backend, "set_reply_out", None)
        if setter_reply:
            setter_reply(on_reply)
        setter_control = getattr(self.backend, "set_voice_leave_out", None)
        if setter_control is not None:
            setter_control(lambda: current() and self.request_voice_leave())
        setter_volume = getattr(self.backend, "set_voice_volume_out", None)
        if setter_volume is not None:
            async def on_volume(command: VolumeCommand) -> dict[str, Any]:
                if not current():
                    return {"ok": False, "error": "语音会话已变化"}
                return await self._apply_volume_command(command)
            setter_volume(on_volume)

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

    async def leave(self, *, source: str = "manual", expected_visit_id: str | None = None,
                    expected_operation_epoch: int | None = None) -> dict[str, Any]:
        self._ensure_operations()
        if expected_operation_epoch is not None and expected_operation_epoch != self._operation_epoch:
            return {"ok": False, "error": "stale operation"}
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
            was_joined = self._joined
            event = VoiceOperation("leave", source, self._area, self._channel, self._visit_id)
            self._operation_epoch += 1
            result = await self._leave_locked(source=source)
            if result["ok"]:
                self._join_source = ""
                self._visit_id = ""
        if result["ok"] and was_joined:
            await self._emit_operation(event)
        return result

    async def _leave_locked(self, *, source: str = "system") -> dict[str, Any]:
        retired_error = await self._drain_retired_channels()
        if retired_error is not None:
            return {"ok": False, "error": retired_error}
        if source == "system":
            self._voice_suspended = True
        self._operation_epoch += 1
        await self._stop_round_worker()
        await self._cancel_reply_tasks()
        self._discard_live_audio = True
        self._speaking = False
        self._reply_generating = False
        if source == "system" and self.duplex is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.duplex.enable_listen(False), 1.0)
        aclose = getattr(self.backend, "aclose", None)
        if aclose is not None:
            try:
                # 先等已受理推送终态，再使用独立预算关闭模型 reader。
                async with self._live_output_lock:
                    pass
                await asyncio.wait_for(aclose(), 3.0)
            except Exception as exc:
                return {"ok": False, "error": f"voice session close failed: {exc}"}
        session = self.channels.snapshot().session
        if session is not None:
            outcome = await self.channels.execute(LeaveRoom(uuid4().hex, session))
            if outcome.status != "completed":
                return {"ok": False, "error": f"voice leave failed: {outcome.reason}"}
        # 退房就关掉 Live 会话：否则 Gemini 会话会一直挂着（既计费又占并发），
        # 而且下次进房时 start_session 会因为旧会话状态而变成空操作。
        self.context_bridge.clear()
        self._ensure_output_controls().reset_mute()
        self._retiring_visit_id = ""
        self._reply_generating = False
        self._speaking = False
        self._rearm_barge_in()
        self._user_buffers.clear()
        self._vad.reset()
        return {"ok": True}

    def status(self) -> dict[str, Any]:
        return {
            "astrbot_context": self._ensure_context_bridge().status(),
            "voice_observation": self.backend.observation.snapshot() if hasattr(self.backend, "observation") else {"recent_decisions": [], "limit": 40},
            "enabled": self.settings.enabled,
            "model_connection": self.backend.connection_status() if isinstance(self.backend, ConnectionReporter) else None,
            "conversation_window_seconds": self.settings.conversation_window_seconds if self.settings.conversation_window_enabled else 0,
            "backend": self.settings.backend,
            "mode": "live" if self.live_mode else "cascade",
            "joined": self._joined,
            "room_state": self.channels.snapshot().state.value,
            "room_cleanup_pending": bool(getattr(self, "_retired_channels", [])),
            "pending_output_confirmations": len(getattr(self, "_completion_tasks", set())),
            "join_source": getattr(self, "_join_source", ""),
            "area": self._area,
            "channel": self._channel,
            "speaking": self._speaking,
            "output_volume": self.settings.output_volume,
            "output_muted": self.output_muted,
            "turns": self.turns,
            "cascade_attempts": self._ensure_attempt_budget().snapshot().attempts,
            "cascade_turn_limit": self.settings.cascade_turn_limit,
            "last_user_text": self.last_user_text,
            "last_reply": self.last_reply,
            "persona_len": len(self.settings.persona),
        }

    async def wait_for_reply_end(self, timeout: float = 30.0) -> bool:
        self._ensure_operations()

        async def wait() -> bool:
            while (self._reply_generating or self._speaking or self._busy.locked()
                   or bool(self._completion_tasks)
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
        self._ensure_operations()
        await self._cancel_announcement()
        async with self._operation_lock:
            await self._stop_reply_locked(restart=True)

    async def _stop_reply_locked(self, *, restart: bool) -> None:
        self._operation_epoch += 1
        self._discard_live_audio = True
        await self._stop_round_worker()
        await self._cancel_reply_tasks()
        if self.live_mode:
            async with self._live_output_lock:
                pass
            await asyncio.wait_for(self.backend.aclose(), 3.0)
        async with self._live_output_lock:
            if self.duplex is not None:
                await self.duplex.stop_tts()
        self._speaking = False
        self._reply_generating = False
        if self.live_mode:
            # Gemini interrupt is intentionally a no-op. A fresh session is the
            # only safe boundary after a timed-out reply; old audio stays muted.
            if restart and self._joined:
                await self._wire_live()
                assert isinstance(self.backend, SessionStarter), "Live backend requires start_session"
                await asyncio.wait_for(self.backend.start_session(), 5.0)
        elif restart and self._joined:
            await self._start_round_worker()

    async def _cancel_reply_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._reply_tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def speak_announcement(self, kind: str, template: str, *, expected_visit_id: str | None = None,
                                 timeout: float = 20.0) -> dict[str, Any]:
        from dialogue import announcement_instruction
        from voice_agent.announcements import run_announcement

        self._ensure_operations()
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

    def _vad_for(self, uid: str) -> VoiceActivityDetector:
        vad = self._user_buffers.get(uid)
        if vad is None:
            vad = create_vad(
                VadvConfig(
                    sample_rate=self.settings.sample_rate_in,
                    silence_ms=self.settings.silence_ms,
                    max_utterance_ms=self.settings.max_utterance_ms,
                )
            )
            self._user_buffers[uid] = vad
        return vad

    async def _on_remote_pcm(self, uid: str, pcm: bytes, sample_rate: int) -> None:
        if (getattr(self, "_voice_control_task", None) is not None
                or getattr(self, "_announcement_active", False) or getattr(self, "_retiring_visit_id", "")
                or not getattr(self, "_connection_ready", True) or getattr(self, "_voice_suspended", False)):
            return
        if not self.settings.enabled or not pcm:
            return
        uid = str(uid or "unknown")
        # Browser capture identifies audio by Agora's numeric ID. Resolve the
        # room's received identity broadcast before applying Oopz UID filters.
        if uid.isdecimal():
            voice = getattr(getattr(self, "_bot", None), "voice", None)
            states = getattr(voice, "voice_states", lambda: {})()
            for oopz_uid, state in states.items():
                if str(state.get("cid")) == uid:
                    uid = str(oopz_uid)
                    break
        _dump_remote_pcm(uid, pcm, sample_rate)
        if self.settings.listen_only_uids and uid not in self.settings.listen_only_uids:
            return

        if self.live_mode:
            # Live：持续灌入，模型自己做回合。
            #
            # 本会话**关掉了服务端的自动抢话**（见 gemini_live._realtime_input_config）：
            # 灌进去的是整个房间的混流，服务端只要认出「有人在说话」就取消生成，
            # 实测从 0.02 的远处背景人声到 0.15 的贴麦**任何音量都会打断 bot**，
            # 热闹的房间里 bot 一句完整的话都说不完。所以抢话只能在这里判。
            #
            # 判据是「某人**连续说话**够久」而不是「有人出声」：一声咳嗽、一次键盘、
            # 一个「嗯」累计不到 barge_in_hold_ms 就清零，bot 继续说。
            #
            # 仍然**每回合最多打断一次**（_barge_in_armed）：这是每个远端音频帧
            # （20ms 一帧、房间里每个人）都会走的路径，不设闸门的话模型每吐一个
            # 分片就被下一帧掐掉，一次 4.4 秒的回复被剁成 13 段碎片并夹 13 次
            # 30ms 静音，听感是「能听见但听不清」（2.0.3 修的就是这个）。
            if (
                self.settings.barge_in
                and (self._speaking or bool(self._completion_tasks))
                and self._barge_in_reached(uid, pcm, sample_rate)
            ):
                # 服务端 NO_INTERRUPTION 会继续生成旧回合；直到回合边界都丢弃。
                self._discard_live_audio = self._speaking or self._reply_generating
                for playback in tuple(self._playbacks):
                    playback.interrupt()
                self._playback_epoch += 1
                interrupt = getattr(self.backend, "interrupt", None)
                if interrupt is not None:
                    await interrupt()
                # 推流可能仍在等待浏览器创建轨道，清队列须等在途分片入队后执行。
                async with self._live_output_lock:
                    if self.duplex is not None:
                        with contextlib.suppress(Exception):
                            await self.duplex.stop_tts()
                    self._speaking = False
            push = getattr(self.backend, "push_audio", None)
            if push is not None:
                note_speaker = getattr(self.backend, "note_input_speaker", None)
                if note_speaker is not None and pcm16_rms(pcm) >= _MIN_UTTERANCE_RMS:
                    note_speaker(uid)
                await push(pcm, sample_rate)
            return

        # 级联：VAD 切句
        if (self._speaking and self.settings.barge_in
                and (not isinstance(self.backend, StreamingUtterance)
                     or self._barge_in_reached(uid, pcm, sample_rate))):
            await self._interrupt_speaking()
        if self._cascade_limit_reached():
            return
        vad = self._vad_for(uid)
        utterance = vad.feed(pcm)
        if utterance:
            # 底噪闸门。EnergyVad 只按 30ms 帧的 RMS 过不过 energy_threshold
            # (0.012) 判「有人在说话」，没人开口时若某人麦克风底噪高过它就够
            # 触发 —— 于是噪声被切成一句句「人话」送去 ASR，而 MiMo 这类对话式
            # ASR 对非人声不会返回空，会**编**一句像样的转写（实测最常见的是
            # 「嗯。」），结果就是「房间里没人说话，bot 却一直在接话」。
            #
            # 阈值取自真实录音的实测分布（C:\APP\pcm_dump\agent.pcm，1305 个
            # 1 秒窗）：真说话 RMS 0.03~0.22，会误触发 VAD 的带在 0.012~0.02。
            # 取 0.02 正好卡在两者之间。若哪天发现真说话被丢掉，日志里会打出
            # 每次丢弃的 RMS，按它调这个常量即可。
            energy = pcm16_rms(utterance)
            if energy < _MIN_UTTERANCE_RMS:
                logger.info(
                    "丢弃疑似噪声回合：RMS=%.4f（%.1fs 音频，阈值 %.3f）",
                    energy,
                    len(utterance) / 2 / max(1, sample_rate),
                    _MIN_UTTERANCE_RMS,
                )
                return
            # 时长闸门。和上面那道**能量**闸门互补：一声音量够大但只有 0.1 秒的
            # 「对。」「哦。」能量完全达标，会一路走到 ASR/LLM/TTS，回一句
            # 「嗯嗯，知道了。」—— 用户听到的就是「它每句都接」。
            # 实测（2026-10-01 的 58 个回合）这类占相当一部分，且全是噪音。
            min_ms = max(0, int(getattr(self.settings, "min_utterance_ms", 0) or 0))
            if self.settings.voice_leave_enabled or self.settings.voice_volume_enabled:
                # Short commands still reach intent detection. The cascade keeps
                # the original threshold for non-command chat replies after ASR.
                min_ms = min(min_ms, VOICE_CONTROL_MIN_MS)
            audio_ms = len(utterance) / 2 / max(1, sample_rate) * 1000.0
            if audio_ms < min_ms:
                logger.info(
                    "丢弃过短回合：%.2fs（阈值 %.2fs，一个字的气声）",
                    audio_ms / 1000.0,
                    min_ms / 1000.0,
                )
                return
            self._offer_utterance(uid, utterance, sample_rate)

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
        try:
            await self._handle_utterance(item.user_key, item.pcm, item.sample_rate)
        finally:
            self.reply_scheduler.finish(item, cooldown=self._reply_cooldown(), interrupted=self._barged_in)

    def _ensure_barge_gate(self):
        if not hasattr(self, "_barge_gate"):
            self._barge_gate = create_barge_in_gate()
        return self._barge_gate

    def _rearm_barge_in(self) -> None:
        self._ensure_barge_gate().rearm()
        self._discard_live_audio = False

    def _barge_in_reached(self, uid: str, pcm: bytes, sample_rate: int) -> bool:
        rate = int(sample_rate or self.settings.sample_rate_in) or 16000
        return self._ensure_barge_gate().observe(
            SpeechActivity(uid, pcm16_rms(pcm) >= _BARGE_IN_ENERGY,
                           len(pcm) / 2 / rate * 1000),
            hold_ms=int(getattr(self.settings, "barge_in_hold_ms", 0) or 0),
        )

    async def _interrupt_speaking(self) -> None:
        self._speaking = False
        # 记下来：本回合是被抢话打断的，结束时不计冷却（见行为调度器）。
        self._barged_in = True
        playbacks = tuple(self._playbacks)
        for playback in playbacks:
            playback.interrupt()
        stream_task = getattr(self, "_stream_reply_task", None)
        if stream_task is not None and not stream_task.done():
            stream_task.cancel()
        try:
            if playbacks:
                for playback in playbacks:
                    await playback.stop()
            elif self.duplex is not None:
                await self.duplex.stop_tts()
        except Exception:
            logger.debug("stop tts on barge-in failed", exc_info=True)

    async def _handle_utterance(self, uid: str, pcm: bytes, sample_rate: int) -> None:
        self._ensure_operations()
        task = asyncio.current_task()
        if task is not None:
            self._reply_tasks.add(task)
        try:
            await self._handle_utterance_reply(uid, pcm, sample_rate)
        finally:
            if task is not None:
                self._reply_tasks.discard(task)

    def _cascade_limit_reached(self) -> bool:
        return not self.live_mode and self._ensure_attempt_budget().exhausted(self.settings.cascade_turn_limit)

    async def _record_output(self, user_text: str, reply_text: str, uid: str,
                             session: SessionRef | None, confirmation: Literal["completed", "silent", "interrupted", "failed", "unknown"],
                             submission: TurnSubmission | None = None) -> None:
        if session is None or self.channels.snapshot().session != session:
            return
        # 记录绑定产生它的会话；不把迟到输出归入新房间或新 AstrBot 来源。
        key = uid if uid.startswith("oopz:") or uid == "api" else f"oopz:{uid}"
        turn = ConfirmedTurn(user_text, reply_text if confirmation == "completed" else "",
                             confirmation, user_key=key,
                             channel_key=f"{session.room_target.area}/{session.room_target.channel}")
        if submission is None:
            submission = create_turn_submission(getattr(self, "memory", None),
                                                self._ensure_context_bridge(), speaker=uid)
        await submission.record(turn)

    async def _handle_utterance_reply(self, uid: str, pcm: bytes, sample_rate: int) -> None:
        session = self.channels.snapshot().session
        bridge = self._ensure_context_bridge()
        source_session, epoch = bridge.session_id, self._operation_epoch
        if not pcm or getattr(self, "_announcement_active", False):
            return
        async with self._busy:
            if getattr(self, "_announcement_active", False) or self._cascade_limit_reached():
                return
            # Reserve before the first network await, so queued work and failures
            # cannot exceed the per-join budget. Manual speak does not consume it.
            if session is None:
                return
            permit = self._ensure_attempt_budget().admit(session, self.settings.cascade_turn_limit)
            if isinstance(permit, AttemptRejected):
                return
            if isinstance(self.backend, StreamingUtterance):
                await self._handle_streamed_reply(uid, pcm, sample_rate)
                return
            memory = getattr(self, "memory", None)
            submission = create_turn_submission(memory, bridge, speaker=uid)
            recognized_text = ""
            recorded = False

            def on_recognition(text: str) -> None:
                nonlocal recognized_text
                recognized_text = text

            try:
                try:
                    reply = await self.backend.handle_utterance(
                        pcm,
                        sample_rate=sample_rate,
                        user_key=f"oopz:{uid}",
                        channel_key=f"{self._area}/{self._channel}",
                        on_recognition=on_recognition,
                    )
                except RecognizedGenerationFailed as exc:
                    self.last_reply = f"[error] {exc}"
                    if epoch == self._operation_epoch and bridge.session_id == source_session:
                        await self._record_output(exc.user_text, "", uid, session, "failed", submission)
                        recorded = True
                    logger.exception("event=voice.generation.failed stage=after_recognition")
                    return
                except Exception as exc:
                    logger.exception("voice backend failed")
                    self.last_reply = f"[error] {exc}"
                    return

                if (epoch != self._operation_epoch or self.channels.snapshot().session != session
                        or bridge.session_id != source_session):
                    return

                # 级联这条链路以前**一句话都不记**（mimo_cascade 整份文件只有一条
                # debug），出问题只能靠 WebUI 的 last_user_text/last_reply 两个字段猜，
                # 看不到识别历史、也看不出 VAD 是不是把句子切碎了。音频时长放在这里
                # 就是为了这个：识别结果正常但时长只有 0.3 秒，那是 VAD 在切句，不是
                # 模型听不懂。
                logger.info(
                    "event=voice.asr.completed input_chars=%d audio_seconds=%.1f reply_chars=%d",
                    len(reply.user_text or ""),
                    len(pcm) / 2 / max(1, sample_rate),
                    len(reply.text or ""),
                )
                if epoch != self._operation_epoch or self.context_bridge.session_id != source_session:
                    return
                self.last_user_text = reply.user_text or self.last_user_text
                if reply.control is not None and reply.control.leave:
                    await self._record_output(reply.user_text, "", uid, session, "silent", submission)
                    recorded = True
                    self.request_voice_leave()
                    return
                if reply.control is not None and reply.control.volume is not None:
                    await self._record_output(reply.user_text, "", uid, session, "silent", submission)
                    recorded = True
                    result = await self._apply_volume_command(reply.control.volume)
                    if not self.output_muted:
                        await self.speak_text(result.get("message") or "语音输出调整失败，请查看控制台。")
                    return
                if reply.text:
                    self.last_reply = reply.text

                confirmation: Literal["completed", "silent", "interrupted", "failed", "unknown"] = "silent"
                if reply.pcm16 and self.duplex is not None:
                    self._speaking = True
                    self.turns += 1
                    playback = self._start_playback(session, self.duplex)
                    try:
                        chunk = max(2, int(reply.sample_rate * 0.04) * 2)
                        for offset in range(0, len(reply.pcm16), chunk):
                            if not self._speaking:
                                playback.interrupt()
                                break
                            pushed = await playback.push(reply.pcm16[offset:offset + chunk], reply.sample_rate)
                            if not pushed.ok:
                                logger.warning("模型音频推流失败，房间听不到：%s", pushed.reason)
                                break
                            await asyncio.sleep(0.02)
                        confirmation = (await playback.finish(reply.sample_rate)).status
                    finally:
                        self._playbacks.discard(playback)
                        self._speaking = False
                if confirmation == "completed" and not reply.text:
                    confirmation = "unknown"
                if epoch == self._operation_epoch and bridge.session_id == source_session:
                    await self._record_output(reply.user_text or "", reply.text, uid, session, confirmation, submission)
                    recorded = True
            finally:
                if not recorded and recognized_text:
                    turn = ConfirmedTurn(recognized_text, "", "interrupted", user_key=f"oopz:{uid}",
                                         channel_key=f"{session.room_target.area}/{session.room_target.channel}")
                    await submission.record(turn)

    async def _handle_streamed_reply(self, uid: str, pcm: bytes, sample_rate: int) -> None:
        session = self.channels.snapshot().session
        backend, duplex, epoch = self.backend, self.duplex, self._operation_epoch
        source_session = self._ensure_context_bridge().session_id
        assert isinstance(backend, StreamingUtterance)
        sent = False
        failed = False
        bridge, memory = self.context_bridge, self.memory
        submission = create_turn_submission(memory, bridge, speaker=uid)
        recognized_text = ""
        recorded = False

        def on_recognition(text: str) -> None:
            nonlocal recognized_text
            recognized_text = text

        playback = self._start_playback(session, duplex) if duplex is not None else None
        self._barged_in = False

        async def on_audio(data: bytes, rate: int) -> None:
            nonlocal sent
            if (self._barged_in or epoch != self._operation_epoch or duplex is None
                    or duplex is not self.duplex or not self._connection_ready):
                raise VoiceStreamInterrupted()
            if not sent:
                self.turns += 1
                sent = True
                self._rearm_barge_in()
                self._speaking = True
            frame = max(2, int(rate * 0.04) * 2)
            for offset in range(0, len(data), frame):
                if self._barged_in or epoch != self._operation_epoch:
                    raise VoiceStreamInterrupted()
                async with self._live_output_lock:
                    if self._barged_in or epoch != self._operation_epoch:
                        raise VoiceStreamInterrupted()
                    assert playback is not None
                    pushed = await playback.push(data[offset:offset + frame], rate)
                    result = {"ok": pushed.ok, "error": pushed.reason}
                if not result.get("ok"):
                    raise RuntimeError("语音音频推送失败")
                await asyncio.sleep(0.02)

        try:
            stream_task = asyncio.create_task(backend.handle_utterance_stream(
                pcm, sample_rate=sample_rate, user_key=f"oopz:{uid}",
                channel_key=f"{self._area}/{self._channel}", on_audio=on_audio, on_recognition=on_recognition,
            ))
            self._stream_reply_task = stream_task
            try:
                reply = await stream_task
            except asyncio.CancelledError:
                if self._barged_in:
                    raise VoiceStreamInterrupted() from None
                raise
            finally:
                if self._stream_reply_task is stream_task:
                    self._stream_reply_task = None
            if epoch != self._operation_epoch or self.context_bridge.session_id != source_session:
                return
            self.last_user_text = reply.user_text or self.last_user_text
            if reply.control is not None and reply.control.leave:
                await self._record_output(reply.user_text, "", uid, session, "silent", submission)
                recorded = True
                self.request_voice_leave()
                return
            if reply.control is not None and reply.control.volume is not None:
                await self._record_output(reply.user_text, "", uid, session, "silent", submission)
                recorded = True
                result = await self._apply_volume_command(reply.control.volume)
                if not self.output_muted:
                    await self.speak_text(result.get("message") or "语音输出调整失败")
                return
            self.last_reply = reply.text or self.last_reply
            confirmation: Literal["completed", "silent", "interrupted", "failed", "unknown"] = "silent"
            if playback is not None:
                if self._barged_in:
                    playback.interrupt()
                confirmation = (await playback.finish(reply.sample_rate)).status
            if confirmation == "completed" and not reply.text:
                confirmation = "unknown"
            if epoch == self._operation_epoch and self.context_bridge.session_id == source_session:
                await self._record_output(reply.user_text, reply.text, uid, session, confirmation, submission)
                recorded = True
            logger.info("流式语音回合完成：已输出音频=%s", sent)
        except VoiceStreamInterrupted:
            failed = True
            logger.info("流式语音回合已打断")
        except asyncio.CancelledError:
            failed = True
            raise
        except RecognizedGenerationFailed as exc:
            failed = True
            self.last_reply = f"[error] {exc}"
            if epoch == self._operation_epoch and self.context_bridge.session_id == source_session:
                await self._record_output(exc.user_text, "", uid, session, "failed", submission)
                recorded = True
            logger.exception("event=voice.generation.failed stage=after_recognition")
        except Exception as exc:
            failed = True
            self.last_reply = f"[error] {exc}"
            logger.exception("流式语音回合失败")
        finally:
            try:
                if not recorded and recognized_text and session is not None:
                    turn = ConfirmedTurn(recognized_text, "", "interrupted", user_key=f"oopz:{uid}",
                                         channel_key=f"{session.room_target.area}/{session.room_target.channel}")
                    await submission.record(turn)
            finally:
                if playback is not None:
                    self._playbacks.discard(playback)
                if epoch == self._operation_epoch:
                    self._speaking = False
                    if failed and duplex is not None and duplex is self.duplex:
                        with contextlib.suppress(Exception):
                            await duplex.stop_tts()

    async def speak_text(self, text: str) -> dict[str, Any]:
        self._ensure_operations()
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
            speak = getattr(self.backend, "speak_text", None)
            if speak is not None:
                await speak(text)
                self.last_reply = text
                # 轮次不在这里计：模型出声后会推 turnComplete，由 on_turn_end 计一次。
                # 这里也 +1 的话，一次 /voice/speak 会被记成两轮。
                return {"ok": True, "chars": len(text), "mode": "live"}

        if not hasattr(self.backend, "tts"):
            return {"ok": False, "error": "backend does not support tts"}
        duplex, epoch = self.duplex, self._operation_epoch
        pcm, rate = await self.backend.tts(text)  # type: ignore[attr-defined]
        if not pcm or duplex is None or self.output_muted:
            return {"ok": False, "error": "没有可播放的音频输出"}
        self._speaking = True
        playback = self._start_playback(session, duplex)
        try:
            chunk = max(2, int(rate * 0.04) * 2)
            for offset in range(0, len(pcm), chunk):
                if (not self._speaking or epoch != self._operation_epoch
                        or duplex is not self.duplex):
                    return {"ok": False, "error": "语音输出已取消"}
                pushed = await playback.push(pcm[offset:offset + chunk], rate)
                if not pushed.ok:
                    return {"ok": False, "error": "语音音频推送失败"}
                await asyncio.sleep(0.02)
            evidence = await playback.finish(rate)
            if (evidence.status != "completed" or epoch != self._operation_epoch
                    or duplex is not self.duplex):
                return {"ok": False, "error": "语音播放未正常完成"}
            await self._record_output("", text, "api", session, "completed")
        finally:
            self._playbacks.discard(playback)
            if epoch == self._operation_epoch:
                self._speaking = False
        self.last_reply = text
        self.turns += 1
        return {"ok": True, "chars": len(text), "pcm_bytes": len(pcm), "mode": "cascade"}

    async def refresh_astrbot_context(self) -> dict[str, Any]:
        self._ensure_operations()
        async with self._operation_lock:
            if not self._joined:
                self.context_bridge.clear()
                return {"astrbot_context": self.context_bridge.status()}
            await self._stop_reply_locked(restart=False)
            await self.context_bridge.prepare(self._area, self._channel)
            if self.live_mode:
                await self._wire_live()
                start = getattr(self.backend, "start_session", None)
                if start is not None:
                    await start()
            else:
                await self._start_round_worker()
            return {"astrbot_context": self.context_bridge.status()}

    def update_persona(self, persona: str) -> None:
        self.settings.persona = str(persona or self.settings.persona)
        self.memory.set_persona(self.settings.persona)
