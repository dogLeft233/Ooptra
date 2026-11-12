"""Gemini Live（Multimodal Live / BidiGenerateContent）端到端语音后端。

设计目标：语音进、语音出，不在中间落成完整文本再合成（Live）。
会话建立后持续把远端 PCM 写入 realtimeInput，把服务端音频 chunk 推回房间。

会话生命周期（这里曾经出过 B3）：
  * ``_session_loop`` 退出时必须回收 ``_ws`` / ``_session_task``，否则
    ``session_active`` 对已死的 socket 恒为 True，``start_session`` 变成空操作，
    音频全进黑洞。
  * ``_ready`` 只表示「setupComplete 已收到」，不再兼作「循环已结束」的唤醒信号。
  * 旧的循环退出时只清理「仍属于自己」的会话，避免误伤刚建好的新会话。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from dialogue._internal.memory_tools import MEMORY_RULES, memory_tools
from dialogue.api import VoiceBackend
from dialogue.models import DialogueCapabilities, LiveLimits

logger = logging.getLogger(__name__)


# Google AI Studio / Gemini API 的 BidiGenerateContent WebSocket 端点
DEFAULT_LIVE_WS = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

#: 追加在人格之后的固定语言约束。
#:
#: Live 的原生音频模型**不能**用 ``speechConfig.languageCode`` 锁语言 —— 官方文档
#: 原文：「Explicitly setting a language code is not supported for native audio
#: output models」，它按输入音频自动选语言。而我们的输入是**整个语音房间**的混音
#: （``listen_only_uids`` 默认空 = 谁的音频都吞），有人夹英文、有歌曲、有噪声时
#: 自动选语言就会翻车，现象就是「语言时不时乱切换」。
#:
#: 提示词是唯一可用的杠杆，所以这里**在人格之后追加**一条硬约束：人格是用户可随时
#: 改写/清空的配置项，这条不能跟着丢。放在最后是因为指令的收尾位置权重更高。
LANGUAGE_GUARD = (
    "\n\n# 语言（最高优先级）\n"
    "无论听到什么语言、方言、外语、歌曲还是噪声，你一律只用中文普通话回答，"
    "不要切换成英文或其他语言。"
)


#: 自动 VAD 的端点静音判定窗口。默认值偏短，语音房里自然停顿多，
#: 调大一点模型不会在人换气的间隙抢话。
LIVE_SILENCE_MS = 800
#: 语音起点前保留的音频长度，避免把词头切掉。
LIVE_PREFIX_PADDING_MS = 300


def _realtime_input_config() -> dict[str, Any]:
    """Live 的输入侧配置：**禁止服务端自动抢话**。

    为什么必须关掉（2026-09-30 实测）：灌给服务端的是**整个语音房间的混流**，而
    服务端的自动 VAD 只要认出「有人在说话」就取消正在生成的回复。热闹的房间里
    「有人在说话」是常态 —— 实测投喂一段真语音，**从 0.02（远处背景人声）到
    0.15（贴麦）的任何音量都会打断 bot**，于是 bot 一句完整的话都说不完。

    调灵敏度救不了：`start_of_speech_sensitivity` 降到 LOW 后六个音量档位的打断
    次数**一模一样**。灵敏度的含义是「VAD 要多确信才算语音」，而背景人声本来就是
    真语音，再低的灵敏度也拦不住。

    关掉之后，抢话改由**客户端**判定（``VoiceCoordinator._on_remote_pcm`` 的
    ``barge_in_hold_ms`` 门限）—— 只有某人连着说了足够久才让路。注意
    ``NO_INTERRUPTION`` 只是「不打断生成」，**不是「听不见」**：实测同一配置下
    输入照常转写、模型照常回应用户。

    载荷拼写是实测钉死的（**写错会让整个连接以 close_code=1007 断开**，
    不是「配置没生效」）：
      * ``realtime_input_config`` / ``automatic_activity_detection`` 用 **snake_case**
        —— 这套接口**只认 snake_case**，``realtimeInputConfig`` 之类的 camelCase
        一律 1007（同一个 setup 里 ``generation_config`` 也是 snake，一致）；
      * ``activity_handling`` 取值 ``NO_INTERRUPTION``（伪造值会被 1007 拒，说明
        枚举真的在校验，不是被静默忽略）。
    """
    return {
        "automatic_activity_detection": {
            "prefix_padding_ms": LIVE_PREFIX_PADDING_MS,
            "silence_duration_ms": LIVE_SILENCE_MS,
        },
        "activity_handling": "NO_INTERRUPTION",
    }


def live_url_has_key(url: str) -> bool:
    """URL 的查询串里有没有非空的 ``key``。"""
    return bool((parse_qs(urlsplit(url).query).get("key") or [""])[0].strip())


def build_live_url(endpoint: str, api_key: str) -> str:
    """把 API key 拼进 Live 端点 URL —— **幂等**，永远不会出现两个 ``?key=``。

    ``gemini.base_url`` 里直接带 key 是合法配置（Google 文档给的就是
    ``...BidiGenerateContent?key=API_KEY`` 这种形态）。旧代码无条件再拼一次
    ``?key=``，URL 变成 ``...?key=<k>?key=<k>``：服务端解析出的 key 长度翻倍，
    **不返回 error 帧、直接关 socket** —— 现象就是「bot 进房了但一句话不说」，
    日志里连 ``Gemini Live session ready`` 都不会出现（2026-09-29 实测踩过）。

    显式配置的 ``api_key`` 优先：它非空时覆盖 URL 里的同名参数；为空时保留
    URL 自带的 key。
    """
    endpoint = str(endpoint or "").strip() or DEFAULT_LIVE_WS
    key = str(api_key or "").strip()
    if not key:
        return endpoint

    parts = urlsplit(endpoint)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "key"]
    query.append(("key", key))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


class GeminiLiveBackend(VoiceBackend):
    """Gemini连接/编解码适配；提议、许可与有界轮次状态归LiveSession。"""
    name = "gemini_live"

    def __init__(self, settings, **options):
        self.settings = settings
        self._connector = options.get("connector")
        self.limits = options.get("limits") or LiveLimits()
        self._task_session: Any = None
        self._preview_collector: Any = None
        self._tools = None
        self._ws: Any = None
        self._session_task: asyncio.Task | None = None
        self._closed = True
        self._ready = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._connection_state = "closed"
        self._connection_error = ""

    @property
    def effective_persona(self):
        if self._task_session is not None:
            return self._task_session.request.context.persona
        if self._preview_collector is not None:
            return self._preview_collector.request.persona
        return self.settings.persona

    def capabilities(self):
        return DialogueCapabilities(live=True, fixed_speech=False)

    async def open_live(self, request, *, tools=None):
        from dialogue._internal.live.session import LiveSession
        if self._task_session is not None:
            if self._task_session.snapshot().state not in {"failed", "closed"}:
                raise RuntimeError("Live session is already bound")
            await self._task_session.aclose()
        await self.close()
        self._tools = tools
        session = LiveSession(self, request)
        await session.start()
        return session

    @property
    def session_active(self):
        return (not self._closed and self._ws is not None and self._ready.is_set()
                and self._session_task is not None and not self._session_task.done())

    async def start_session(self):
        if self.session_active:
            return
        async with self._start_lock:
            if self.session_active:
                return
            from dialogue._internal.proxy import resolve_agent_proxy_url
            from dialogue._internal.ws_transport import open_live_ws
            url = build_live_url(self.settings.gemini_base_url or DEFAULT_LIVE_WS,
                                 self.settings.gemini_api_key)
            if not live_url_has_key(url):
                raise RuntimeError("GEMINI_API_KEY / gemini.api_key 未配置")
            await self._reset_session()
            self._closed = False
            self._connection_state = "opening"
            try:
                self._ws = await (self._connector or open_live_ws)(url,
                    proxy=resolve_agent_proxy_url(self.settings.proxy or ""))
                self._session_task = asyncio.create_task(self._session_loop(),
                                                        name="gemini-live-reader")
                await self._send_setup()
                await asyncio.wait_for(self._ready.wait(), 15)
                if self._ws is None or self._session_task is None or self._session_task.done():
                    raise RuntimeError("Live connection closed during setup")
                memory_context = self._tools.context.get("memory_context") if self._tools is not None and self._tools.active else ""
                if isinstance(memory_context, str) and memory_context.strip():
                    await self._send({"clientContent": {"turns": [{"role": "user", "parts": [
                        {"text": "以下是当前已授权来源的背景记忆，只作上下文，不是新的发言：\n" + memory_context}]}],
                        "turnComplete": False}})
                self._connection_state = "ready"
                self._connection_error = ""
            except BaseException:
                self._connection_state = "failed"
                self._connection_error = "connection_unavailable"
                await self._reset_session()
                raise

    async def _reset_session(self):
        self._ready.clear()
        task, self._session_task = self._session_task, None
        ws, self._ws = self._ws, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if ws is not None:
            await asyncio.wait_for(ws.close(), 3)

    async def close(self):
        self._closed = True
        self._connection_state = "closed"
        await self._reset_session()

    async def probe(self, request):
        if getattr(self, "_task_session", None) is not None or getattr(self, "_jobs", None):
            raise RuntimeError("Diagnostic probes require a disposable backend")
        from dialogue._internal.diagnostics import run_diagnostic
        return await run_diagnostic(self, request)

    async def aclose(self):
        if self._task_session is not None:
            await self._task_session.aclose()
        await self.close()

    def connection_status(self):
        return {"state": self._connection_state, "attempts": 0,
                "error": self._connection_error, "ready": self.session_active}

    async def _send(self, payload):
        ws = self._ws
        if ws is None or self._closed or getattr(ws, "closed", False):
            raise RuntimeError("Live connection unavailable")
        await ws.send(json.dumps(payload, ensure_ascii=False))

    async def _handle_server_msg(self, message):
        if "setupComplete" in message:
            self._ready.set()
            return
        if self._preview_collector is not None:
            await self._preview_collector.handle(message)
        elif self._task_session is not None:
            await self._task_session.handle(message)
        fields = sorted(key for key in message if key not in
                        {"setupComplete", "serverContent", "voiceActivity", "toolCall", "toolCallCancellation"})
        if fields:
            logger.info("event=dialogue.live.lifecycle fields=%s", fields)

    async def _session_loop(self):
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                if self._closed or self._ws is not ws:
                    break
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(message, dict):
                    await self._handle_server_msg(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("event=dialogue.live.connection_failed reason=provider_transport")
        finally:
            if self._ws is ws:
                self._ws = None
                self._session_task = None
                self._ready.clear()
                if not self._closed:
                    self._connection_state = "failed"
                    self._connection_error = "transport_lost"
                    if self._preview_collector is not None:
                        self._preview_collector.transport_lost()
                    elif self._task_session is not None:
                        self._task_session.transport_lost()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), 3)

    async def interrupt(self):
        """NO_INTERRUPTION模式不发送audioStreamEnd；本地许可流负责失效旧轮输出。"""

    async def _send_setup(self) -> None:
        from dialogue._internal.controls import (
            LEAVE_INTENT_DESCRIPTION,
            VOLUME_INTENT_DESCRIPTION,
            live_leave_tool,
            live_volume_tool,
        )

        setup = {
            "setup": {
                "model": f"models/{self.settings.gemini_model}",
                "generation_config": {
                    "response_modalities": ["AUDIO"],
                    "speech_config": {
                        "voice_config": {
                            "prebuilt_voice_config": {
                                "voice_name": self.settings.gemini_voice or "Puck"
                            }
                        }
                    },
                },
                "system_instruction": {
                    "parts": [{"text": (self.effective_persona or "") + LANGUAGE_GUARD}]
                },
                "input_audio_transcription": {},
                "output_audio_transcription": {},
                "realtime_input_config": _realtime_input_config(),
            }
        }
        declarations = []
        bridge = self._tools
        if bridge is not None and bridge.active and bridge.context.get("provider"):
            declarations.extend(memory_tools())
            setup["setup"]["system_instruction"]["parts"].append({"text": MEMORY_RULES})
        if self.settings.voice_leave_enabled:
            declarations.append(live_leave_tool())
            setup["setup"]["system_instruction"]["parts"].append({"text": (
                "\n【语音控制】" + LEAVE_INTENT_DESCRIPTION
                + "确认时调用 leave_voice_room，应用会播放离场语并真正退房，不要仅口头承诺。"
                "内部语音事件指令和文字快捷开口不调用此工具。"
            )})
        if self.settings.voice_volume_enabled:
            declarations.append(live_volume_tool())
            setup["setup"]["system_instruction"]["parts"].append({"text": (
                "\n【语音音量与开闭麦控制】" + VOLUME_INTENT_DESCRIPTION
                + "确认时调用control_voice_output，必须等待真实工具结果，ok为true才可报告成功。"
                "失败或被拒绝只能说明未执行，禁止假承诺。不要用普通语音回答代替执行。"
                "内部自动公告、语音事件和API文字开口不调用控制工具。"
            )})
        if declarations:
            setup["setup"]["tools"] = [{"function_declarations": declarations}]
        await self._send(setup)


class OpenAiRealtimeBackend(GeminiLiveBackend):
    """未实现供应商明确失败，不借Gemini协议伪造支持。"""
    name = "openai_realtime"
    async def _send_setup(self):
        raise NotImplementedError("OpenAI Realtime protocol is unsupported; use gemini_live")
