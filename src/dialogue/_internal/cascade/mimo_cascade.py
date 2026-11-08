"""MiMo 级联：ASR → Chat LLM → TTS（stream + pcm），适合中文语音房。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import struct
import time
import wave
from io import BytesIO

from behavior.models import VoiceControlDecision, parse_control_decision
from context.models import PreparedContext
from dialogue._internal.controls import (
    CONTROL_DECISION_FORMAT,
    LEAVE_INTENT_DESCRIPTION,
    VOLUME_INTENT_DESCRIPTION,
)
from dialogue._internal.memory_tools import MEMORY_RULES, memory_tools
from dialogue._internal.room_context import history_messages, room_rules
from dialogue.api import ContextToolPort, VoiceBackend
from dialogue.models import (
    VoiceStreamInterrupted,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 语音房文本净化
#
# 2026-10-01 实测（把 TTS 产物再喂回 ASR，直接读「房间里听到的是什么」）：
#
#   '🧘'            -> "Somtime, think."        ← 用户报的「bot 会说 think 的内容」
#   '😤'            -> 4.48s 英文乱语
#   '😐'            -> "Yet Ismay's fine."
#   '<no speechdetected>你这说半截卡壳了' -> "No speech detected，你这说半截卡壳了。"
#   ' thinking用户说要短一点<｜end▁of▁thinking｜>好的' -> "Thinking 用户说要短一点。End of thinking 大鱼，好的"
#
# MiMo TTS 遇到 emoji / 尖括号控制标记时不会跳过，而是**照着编一段英文念出来**。
# LLM 又极爱吐 emoji（本机记忆里 9% 的回复带 emoji 或星号动作），所以必须在
# 送进 TTS 之前清干净 —— 这是唯一能保证房间听不到乱码的地方。
# ----------------------------------------------------------------------

# 全角尖括号/竖线 -> 半角，否则 ＜think＞ 这类变体会漏网
_FULLWIDTH_RE = re.compile("[＜＞｜]")

# 思考链标记： thinking / <｜end▁of▁thinking｜> / </thinking>。必须排在通用
# 「< 到 >」规则**之前**：`' thinkingA<｜end▁of▁thinking｜>B'` 里第一个 < 后面
# 紧跟着另一个 <，通用规则匹配不上，会把 "thinking" 当人话留在文本里念出去。
# 收尾的 > 是可选的：`max_tokens` 把回复截断在思考块中间时，开标签就没有闭合的
# `>`，而通用的「< 到 >」规则在这种情况下匹配不上（后面没有 > 可吃），会把
# "thinking" 三个字当人话留在文本里念出去。
_THINK_TAG_RE = re.compile(r"<\s*/?\s*(?:end[▁_]?of[▁_]?)?thinking\s*[▁|]*>?", re.I)

# 通用尖括号控制标记：<no speechdetected>、<|endoftext|> 等
_TAG_RE = re.compile(r"<[^<>]{0,80}>|<\|[^|]{0,80}\|>", re.S)

# 清完仍然孤立的尖括号
_ANGLE_RE = re.compile(r"[<>]")

# emoji / 图形符号 / 旗帜 / 变体选择符 / 零宽连接
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # 麻将、多米诺、扑克、各类图形与表情
    "\U0001f1e6-\U0001f1ff"  # 区域指示符（国旗）
    "☀-➿"          # 杂项符号、装饰符号（☀✅✨✂…）
    "⬀-⯿"          # 杂项符号与箭头
    "←-⇿"          # 箭头
    "️⃣‍"     # 变体选择符、组合键帽、零宽连接
    "]+"
)

# Markdown 装饰字符（星号动作、加粗、标题、引用、代码、表格）
_MD_RE = re.compile(r"[*_`~#>|]+")

# 英文拟声词：TTS 会念成「哎母」，换成中文语气词
_TIC_RE = re.compile(r"\b(em{2,}|hm{2,}|yeah+)\b", re.I)

# 分点编号与项目符号（念出来是「1、」「横杠」，纯噪音）。
# 末尾的 (?!\d) 不能省：`77、78、79、80连嗯` 是正文不是列表，没有它就会把开头的
# `77、` 当标记剥掉，剥完 `78、` 又落到行首 —— 每清一遍少一段，sanitize_for_tts
# 就不再幂等，而 tts() 的兜底正好要再清一遍（存的和念的就对不上了）。
# 字符类里的破折号/圆点用 \uXXXX 转义写：源码里直接写会被 ruff 判为易混字符
_BULLET_RE = re.compile(r"(?m)^\s*(?:\d+[.、)）](?!\d)|[\-\u2013\u2014\u2022\u00b7])\s*")

# ASR 的「没听到人话」标记
_NO_SPEECH_RE = re.compile(
    r"(?i)[\s\W]*(?:no\s*speech(?:\s*detected)?|speech\s*not\s*detected)[\s\W]*"
)

# 换行在 TTS 里就是一个停顿，但规则明写「不要换行」，模型照换不误。
# 换行前面已经是标点的直接并上去（否则会念出「。，」「：，」这种怪组合），
# 其余的把换行当成逗号。字符类必须带上「：」—— 首版漏了它，实测把
# 「E宝：<换行>你这是在…」清成了「E宝：，你这是在…」。
# 见 tests/test_mimo_sanitize.py。必须排在 _BULLET_RE 之后用（它是行锚定的）。
_NL_AFTER_PUNCT_RE = re.compile(r"(?<=[。！？!?…，,、；;：:）)】」》])\s*\n+\s*")
_NL_OTHER_RE = re.compile(r"\s*\n+\s*")

# 清完整条只剩标点 = 本来就没内容，当作空串（TTS 念不出来）
_ONLY_PUNCT = "，,、；;：:"

# 语音房输出规则，追加在 persona 之后。persona 是用户自己的，不动它，
# 这条作为最高优先级的补充约束 —— 见 tests/test_mimo_sanitize.py 的对照数据。
VOICE_ROOM_RULES = (
    "\n\n【语音房输出规则（优先级高于以上全部）】\n"
    "你的回答会被直接念成语音，必须遵守：\n"
    "1. 一到两句话，总共不超过 35 个字；\n"
    "2. 不要换行、不要分点、不要标题、不要加粗、不要任何 Markdown 符号；\n"
    "3. 不要使用 emoji、颜文字、图片或表情符号；\n"
    "4. 不要使用 emm / yeah 这类英文拟声词；\n"
    "5. 宁短勿长，没必要的就不说。"
)

# 兜底长度（字）：prompt 规则之外的最后一道闸，按句号回退，不切半句。
# 规则里写的是「不超过 35 个字」，这里留一点余量；原来的 80 形同虚设 ——
# 记忆里 20% 的回复超过 35 字、最长 150 字，全从这道闸下面漏了过去。
REPLY_CHAR_LIMIT = 45


def sanitize_for_tts(text: str) -> str:
    """把 LLM 回复清成「可以安全念出来」的文本。

    幂等：清过的文本再清一次结果不变（``tts()`` 会再清一遍做兜底）。
    """
    if not text:
        return ""
    t = _FULLWIDTH_RE.sub(lambda m: {"＜": "<", "＞": ">", "｜": "|"}[m.group()], text)
    t = _THINK_TAG_RE.sub(" ", t)
    t = _TAG_RE.sub(" ", t)
    t = _ANGLE_RE.sub(" ", t)
    t = _EMOJI_RE.sub("", t)
    t = _TIC_RE.sub(lambda m: "嗯" if m.group(1)[0] in "eEhH" else "耶", t)
    t = _MD_RE.sub("", t)
    t = _BULLET_RE.sub("", t)
    t = t.replace("\r", "\n")
    t = re.sub(r"[ \t　]+", " ", t)
    # 换行归一：规则里明写不要换行，模型照换 —— 念出来是个突兀的停顿。
    # 前一个字已经是标点就并上去，否则读成一个逗号。处理后不再有 \n。
    t = _NL_AFTER_PUNCT_RE.sub("", t)
    t = _NL_OTHER_RE.sub("，", t)
    t = t.strip()
    if not t.strip(_ONLY_PUNCT):  # 清完只剩标点 = 没内容
        return ""
    return t


def looks_like_reasoning_leak(text: str) -> bool:
    """判断这条回复是不是模型把「内心独白」当正文吐了出来。

    ``thinking: disabled`` 是生效的（reasoning_tokens 实测 41 -> 0），但模型偶尔
    还是会把思考写进 content，实测约 1%（`data/voice_memory.jsonl` 391 条里 2 条）::

        'The user is just saying "Hello" repeatedly. I should respond in a cute, tsundere…'
        'The user said "的人嘛" which seems like a fragment, repeated twice. It doesn\'t make…'

    这种英文长句念出来就是噪音，宁可这一轮不说。判据是「够长 + 基本全是 ASCII
    字母」—— 短句（'Oh,okay.'）放行，中文里夹英文名（'我叫EasonClaw，你可以…'，
    比值 0.42）也放行。
    """
    letters = [c for c in text if not c.isspace()]
    if len(letters) < 20:
        return False
    ascii_letters = sum(1 for c in letters if c.isascii() and c.isalpha())
    return ascii_letters / len(letters) > 0.6


def clamp_reply(text: str, limit: int = REPLY_CHAR_LIMIT) -> str:
    """超长时在最后一个句末标点处截断；找不到就退回省略号，绝不切半句。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(c) for c in "。！？!?…")
    if cut >= limit // 3:
        return head[: cut + 1]
    return head.rstrip() + "…"


def sanitize_asr(text: str) -> str:
    """ASR 结果净化：控制标记一律丢弃，只有标记没有内容时视为「没听到」。"""
    if not text:
        return ""
    t = _FULLWIDTH_RE.sub(lambda m: {"＜": "<", "＞": ">", "｜": "|"}[m.group()], text)
    t = _THINK_TAG_RE.sub(" ", t)
    t = _TAG_RE.sub(" ", t)
    t = _ANGLE_RE.sub(" ", t).strip()
    if not t or _NO_SPEECH_RE.fullmatch(t):
        return ""
    return t


def _aiohttp():
    import aiohttp

    return aiohttp


def pcm16_to_wav(pcm16: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)
    return buf.getvalue()


def wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    with wave.open(BytesIO(data), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if sampwidth != 2:
        # 仅处理 16bit；其它位深粗暴按 8bit/32bit 转换兜底
        if sampwidth == 1:
            samples = [(b - 128) << 8 for b in raw]
            raw = struct.pack("<" + "h" * len(samples), *samples)
        elif sampwidth == 4:
            count = len(raw) // 4
            ints = struct.unpack("<" + "i" * count, raw[: count * 4])
            samples = [max(-32767, min(32767, v >> 16)) for v in ints]
            raw = struct.pack("<" + "h" * len(samples), *samples)
        else:
            raise ValueError(f"unsupported wav sampwidth: {sampwidth}")
    if channels > 1:
        # 简单取左声道
        count = len(raw) // 2
        samples = struct.unpack("<" + "h" * count, raw[: count * 2])
        mono = samples[::channels]
        raw = struct.pack("<" + "h" * len(mono), *mono)
    return raw, rate


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    count = len(pcm) // 2
    samples = struct.unpack("<" + "h" * count, pcm[: count * 2])
    if not samples:
        return b""
    duration = len(samples) / float(src_rate)
    out_len = max(1, int(duration * dst_rate))
    out: list[int] = []
    for i in range(out_len):
        pos = i * (len(samples) - 1) / max(1, out_len - 1)
        i0 = int(pos)
        i1 = min(len(samples) - 1, i0 + 1)
        frac = pos - i0
        val = samples[i0] * (1 - frac) + samples[i1] * frac
        out.append(max(-32767, min(32767, int(val))))
    return struct.pack("<" + "h" * len(out), *out)


def _parse_decision_json(raw: str):
    # Some compatible endpoints wrap valid JSON in a whole Markdown code block.
    # Do not extract objects from prose or relax control-field validation.
    fenced = re.fullmatch(r"\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n```\s*", raw, re.S | re.I)
    return json.loads(fenced.group(1) if fenced else raw)


class MimoCascadeBackend(VoiceBackend):
    name = "mimo_cascade"

    def __init__(self, settings) -> None:
        self.settings = settings
        self._screen_frame = None
        self._session = None  # aiohttp.ClientSession，懒加载
        self._recognitions = {}
        self._jobs = set()
        self._prepared_context: PreparedContext | None = None
        self._audio_sink = None
        self._tools: ContextToolPort | None = None
        self._generation_id = ""

    def _proxy_url(self) -> str | None:
        from dialogue._internal.proxy import resolve_agent_proxy_url

        return resolve_agent_proxy_url(getattr(self.settings, "proxy", "") or "")

    async def _http(self):
        if self._session is None or self._session.closed:
            aiohttp = _aiohttp()
            timeout = aiohttp.ClientTimeout(total=90, sock_connect=15, sock_read=60)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _headers(self) -> dict[str, str]:
        key = (self.settings.mimo_api_key or "").strip()
        if not key:
            raise RuntimeError("MIMO_API_KEY / mimo.api_key 未配置")
        return {
            "api-key": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    async def _chat_completion(self, body: dict, *, stage: str = "LLM") -> dict:
        session = await self._http()
        url = self.settings.mimo_base_url.rstrip("/") + "/chat/completions"
        async with session.post(
            url, headers=self._headers(), json=body, proxy=self._proxy_url()
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"{stage} failed HTTP {resp.status}")
        return data

    async def asr(self, pcm16: bytes, sample_rate: int) -> str:
        wav = pcm16_to_wav(pcm16, sample_rate)
        b64 = base64.b64encode(wav).decode("ascii")
        body = {
            "model": self.settings.mimo_asr_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": f"data:audio/wav;base64,{b64}"},
                        }
                    ],
                }
            ],
            "asr_options": {"language": "auto"},
        }
        session = await self._http()
        url = self.settings.mimo_base_url.rstrip("/") + "/chat/completions"
        async with session.post(
            url, headers=self._headers(), json=body, proxy=self._proxy_url()
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"ASR failed HTTP {resp.status}")
        choices = data.get("choices") or []
        if not choices:
            return ""
        raw = str(((choices[0] or {}).get("message") or {}).get("content") or "")
        clean = sanitize_asr(raw)
        if clean != raw.strip():
            logger.info("event=dialogue.asr.sanitized input_chars=%s output_chars=%s", len(raw), len(clean))
        return clean

    async def chat(self, user_text: str, *, user_key: str = "", _with_intent: bool = False) -> str:
        bridge = self._tools
        source_context = bridge.context if bridge is not None else {}
        source_session = bridge.session_id if bridge is not None else ""
        shared = source_context.get("ok") is True
        tools_available = shared and bool(source_context.get("provider"))

        def check_source() -> None:
            if shared and bridge is not None and (not bridge.active or bridge.session_id != source_session):
                raise VoiceStreamInterrupted("Shared context source was replaced")
        prepared = self._prepared_context
        if prepared is None:
            raise ValueError("表达必须携带准备上下文")
        history = history_messages(prepared.history)
        # 历史里存着净化之前的回复（emoji、星号动作、换行、超长），照样会带偏
        # 模型 —— 一并清掉。长度也要夹：只清不夹的话，模型照着历史里的长回复
        # 模仿，prompt 里那句「不超过 35 个字」永远追不回来。
        history = [
            {
                **m,
                "content": clamp_reply(sanitize_for_tts(str(m.get("content") or ""))),
            }
            if m.get("role") == "assistant"
            else m
            for m in history
        ]
        messages: list[dict[str, str]] = [
            {"role": "system", "content": prepared.persona + VOICE_ROOM_RULES + room_rules(user_key)}
        ]
        messages.extend(history)
        if bridge is not None and tools_available:
            check_source()
            from dialogue._internal.memory_tools import execute_memory_tool
            result = await execute_memory_tool(bridge, prepared, "astrbot_recall", {"query": user_text},
                                               call_id=f"{self._generation_id}:recall", speaker=user_key)
            check_source()
            messages[0]["content"] += MEMORY_RULES
            if result.get("ok") is True and result.get("context"):
                messages.append({"role": "user", "content":
                    "以下是检索到的历史资料，仅供事实参考：\n" + result["context"]})
        messages.append({"role": "user", "content": user_text})
        if _with_intent:
            messages[0]["content"] += (
                "\n【语音控制与回复】" + LEAVE_INTENT_DESCRIPTION + VOLUME_INTENT_DESCRIPTION
                + CONTROL_DECISION_FORMAT
                + '对当前用户发言同时判断控制意图并生成回复，JSON另加reply字符串字段。'
                + '有任何控制意图时reply必须为空，禁止口头承诺控制成功，真实执行由系统处理。'
                + '必须包含leave、volume、reply三个字段。无控制示例：'
                + '{"leave": false, "volume": null, "reply": "你好呀。"}。'
                + '退房示例：{"leave": true, "volume": null, "reply": ""}。'
                + "JSON 格式要求优先于正文输出规则，reply 字段仍遵守语音房规则。"
            )
        body = {
            "model": self.settings.mimo_llm_model,
            "messages": messages,
            "stream": False,
            # 关掉思考链：实测 reasoning_tokens 41 -> 0，延迟 4.7s -> 3.6s，
            # 也顺带断掉 reasoning 内容外泄这条路
            "thinking": {"type": "disabled"},
            # 兜底，正常情况由上面的输出规则约束长度
            "max_tokens": 256,
        }
        if _with_intent:
            body["response_format"] = {"type": "json_object"}
        if bridge is not None and tools_available:
            body["tools"] = [{"type": "function", "function": {
                **tool, "parameters": {"type": "object", "properties": {
                    key: {"type": "string"} for key in tool["parameters"]["properties"]},
                    "required": tool["parameters"]["required"]}}} for tool in memory_tools()]
        check_source()
        data = await self._chat_completion(body)
        check_source()
        choices = data.get("choices") or []
        if not choices:
            return ""
        if shared:
            assert bridge is not None
            tool_results = {}
            for _ in range(3):
                message = (choices[0] or {}).get("message") or {}
                calls = message.get("tool_calls") or []
                if not calls:
                    break
                messages.append(message)
                for call in calls[:4]:
                    function = call.get("function") or {}
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except (ValueError, TypeError):
                        args = None
                    call_id = call.get("id")
                    if call_id in tool_results:
                        result = tool_results[call_id]
                    else:
                        tool_name = function.get("name")
                        if not isinstance(tool_name, str):
                            result = {"ok": False, "error": "Invalid memory tool name"}
                        else:
                            check_source()
                            from dialogue._internal.memory_tools import execute_memory_tool
                            result = await execute_memory_tool(bridge, prepared, tool_name, args,
                                call_id=str(call_id or ""), speaker=user_key.removeprefix("oopz:"))
                            check_source()
                        tool_results[call_id] = result
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                     "content": json.dumps(result, ensure_ascii=False)})
                check_source()
                data = await self._chat_completion(body, stage="Memory tool follow-up")
                check_source()
                choices = data.get("choices") or []
                if not choices:
                    return ""
            if ((choices[0] or {}).get("message") or {}).get("tool_calls"):
                return ""
        raw = str(((choices[0] or {}).get("message") or {}).get("content") or "")
        if _with_intent:
            return raw
        clean = clamp_reply(sanitize_for_tts(raw))
        if looks_like_reasoning_leak(clean):
            # 弃用整轮：说英文内心独白比不说话更糟（用户已经在房里喊「说中文」了）
            logger.warning("event=dialogue.text.rejected reason=reasoning_leak input_chars=%s", len(raw))
            return ""
        if clean != raw.strip():
            logger.info(
                "event=dialogue.text.sanitized input_chars=%s output_chars=%s",
                len(raw),
                len(clean),
            )
        return clean

    async def detect_control_intent(self, user_text: str) -> VoiceControlDecision:
        body = {
            "model": self.settings.mimo_llm_model,
            "messages": [
                {"role": "system", "content": LEAVE_INTENT_DESCRIPTION + VOLUME_INTENT_DESCRIPTION
                 + CONTROL_DECISION_FORMAT},
                {"role": "user", "content": user_text},
            ],
            "stream": False, "thinking": {"type": "disabled"}, "max_tokens": 64,
            "response_format": {"type": "json_object"},
        }
        data = await self._chat_completion(body, stage="voice intent")
        raw = str((((data.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or "")
        try:
            decision = parse_control_decision(_parse_decision_json(raw))
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid control classification") from exc
        return decision

    async def tts(self, text: str) -> tuple[bytes, int]:
        # 兜底：无论谁调用，进 TTS 的文本一律再清一遍
        text = sanitize_for_tts(text)
        if not text:
            return b"", self.settings.sample_rate_out
        body = {
            "model": self.settings.mimo_tts_model,
            "messages": [
                {"role": "user", "content": "自然对话，口语化，不要播音腔。"},
                {"role": "assistant", "content": text},
            ],
            "audio": {"format": "pcm", "voice": self.settings.mimo_tts_voice},
            "stream": True,
        }
        session = await self._http()
        url = self.settings.mimo_base_url.rstrip("/") + "/chat/completions"
        chunks: list[bytes] = []
        audio_bytes = 0
        async with session.post(
            url, headers=self._headers(), json=body, proxy=self._proxy_url()
        ) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"TTS failed HTTP {resp.status}")
            async for line in resp.content:
                if not line:
                    continue
                text_line = line.decode("utf-8", errors="ignore").strip()
                if not text_line.startswith("data:"):
                    continue
                payload = text_line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = ((event.get("choices") or [{}])[0] or {}).get("delta") or {}
                audio = delta.get("audio") or {}
                data_b64 = audio.get("data") if isinstance(audio, dict) else None
                if data_b64:
                    try:
                        chunk = base64.b64decode(data_b64, validate=True)
                        audio_bytes += len(chunk)
                        if audio_bytes > self.settings.sample_rate_out * 2 * 60:
                            raise ValueError("TTS audio exceeded its finite budget")
                        chunks.append(chunk)
                    except binascii.Error as exc:
                        raise ValueError("Invalid provider PCM encoding") from exc
        pcm = b"".join(chunks)
        # MiMo TTS pcm 默认 24k；若长度异常则按配置输出率处理
        return pcm, self.settings.sample_rate_out

    async def rewrite_announcement(self, kind: str, template: str) -> str:
        from dialogue._internal.announcements import announcement_instruction, clean_announcement

        # Deliberately independent of memory.as_messages(): a visit must not
        # introduce either another room's history or fake human utterances.
        body = {
            "model": self.settings.mimo_llm_model,
            "messages": [
                {"role": "system", "content": self.settings.persona + VOICE_ROOM_RULES},
                {"role": "user", "content": announcement_instruction(kind, template)},
            ],
            "stream": False,
            "thinking": {"type": "disabled"},
            "max_tokens": 128,
        }
        data = await self._chat_completion(body, stage="announcement LLM")
        choices = data.get("choices") or []
        raw = str(((choices[0] or {}).get("message") or {}).get("content") or "") if choices else ""
        clean = clean_announcement(raw)
        return "" if looks_like_reasoning_leak(clean) else clean

    async def recognize(self, input, permit):
        import hashlib
        import math
        import time

        from dialogue.models import Recognition
        if input.session != permit.session:
            raise ValueError("识别许可与音频会话不一致")
        if not permit.input_ref or permit.input_ref != input.input_ref:
            raise ValueError("识别许可与输入身份不一致")
        if not permit.attempt_id or input.attempt_id != permit.attempt_id:
            raise ValueError("识别许可与输入尝试不一致")
        if not math.isfinite(permit.expires_at) or permit.expires_at <= time.monotonic():
            raise ValueError("识别许可已过期")
        key = permit.attempt_id
        fingerprint = (input.input_ref, input.session, input.sample_rate,
                       hashlib.sha256(input.pcm16).digest())
        previous = self._recognitions.get(key)
        if previous is not None:
            if previous[0] != fingerprint:
                raise ValueError("同一识别尝试不能绑定不同输入")
            return await asyncio.shield(previous[1])
        if len(self._recognitions) >= 1024:
            for old in tuple(self._recognitions):
                if self._recognitions[old][1].done():
                    del self._recognitions[old]
                    break
            else:
                raise RuntimeError("Recognition capacity exhausted")

        async def run():
            started = time.monotonic()
            try:
                text = await self.asr(input.pcm16, input.sample_rate)
                return Recognition(input.input_ref, input.session, key, input.user_key,
                                   "recognized" if text.strip() else "no_speech", text,
                                   elapsed_ms=round((time.monotonic()-started)*1000, 1))
            except asyncio.CancelledError:
                raise
            except Exception:
                return Recognition(input.input_ref, input.session, key, input.user_key,
                                   "failed", reason="recognition_unavailable",
                                   elapsed_ms=round((time.monotonic()-started)*1000, 1))
        task = asyncio.create_task(run(), name="dialogue-recognition")
        self._recognitions[key] = (fingerprint, task)
        try:
            return await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def interpret_control(self, input):
        import time

        from dialogue.models import ControlInterpretation
        started = time.monotonic()
        try:
            candidate = await asyncio.wait_for(self.detect_control_intent(input.text), 5)
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError):
            return ControlInterpretation("invalid", reason="invalid_provider_result", elapsed_ms=round((time.monotonic()-started)*1000, 1))
        except Exception:
            return ControlInterpretation("unavailable", reason="classification_unavailable", elapsed_ms=round((time.monotonic()-started)*1000, 1))
        return ControlInterpretation("candidate", candidate, elapsed_ms=round((time.monotonic()-started)*1000, 1))

    def generate(self, intent, context, *, tools=None, screen=None):
        import copy

        from dialogue._internal.output import ExpressionJob

        if intent.session != context.context_ref.session:
            raise ValueError("表达意图与准备上下文会话不一致")
        if intent.context_version != context.version:
            raise ValueError("表达意图与准备上下文版本不一致")
        # 每任务独占supplier sink/分句队列/HTTP资源；不通过共享字段切换请求。
        child = type(self)(copy.deepcopy(self.settings))
        if screen is not None and (not self.capabilities().image_input
                or screen.session != intent.session or not 0 <= time.monotonic()-screen.captured_at <= 3):
            raise ValueError("画面须受当前视觉后端支持且来自原会话的最新捕获")
        child._screen_frame = screen
        child._prepared_context = context
        child._tools = tools
        async def produce(emit):
            try:
                async def on_audio(pcm, rate):
                    await emit("audio", pcm16=pcm, sample_rate=rate)
                streaming = child.capabilities().streaming_utterance
                if streaming:
                    child._audio_sink = on_audio
                started = time.monotonic()
                kind = getattr(intent, "kind", "reply")
                if kind == "fixed":
                    text = sanitize_for_tts(intent.text)
                elif kind == "announcement":
                    text = await child.rewrite_announcement(getattr(intent, "announcement_kind", "enter"), intent.text)
                else:
                    text = await child.chat(intent.text, user_key=intent.user_key)
                if not streaming and kind != "fixed":
                    await emit("timing", timings_ms=(("chat", round((time.monotonic()-started)*1000, 1)),))
                if not text.strip():
                    raise RuntimeError("Empty generated expression")
                await emit("text_final", text=text)
                started = time.monotonic()
                pcm, rate = await child.tts(text)
                if not streaming:
                    await emit("timing", timings_ms=(("tts", round((time.monotonic()-started)*1000, 1)),))
                if not pcm:
                    raise RuntimeError("Empty generated audio")
                if not streaming:
                    await on_audio(pcm, rate)
            finally:
                await child.aclose()
        job = ExpressionJob(intent.action_id, intent.session, produce, deadline=intent.deadline)
        child._generation_id = job.generation_id
        self._jobs.add(job)
        job._task.add_done_callback(lambda _: self._jobs.discard(job))
        return job

    async def probe(self, request):
        if getattr(self, "_task_session", None) is not None or getattr(self, "_jobs", None):
            raise RuntimeError("Diagnostic probes require a disposable backend")
        from dialogue._internal.diagnostics import run_diagnostic
        return await run_diagnostic(self, request)

    async def aclose(self) -> None:
        await asyncio.gather(*(job.aclose() for job in tuple(self._jobs)), return_exceptions=True)
        recognition_tasks = [entry[1] for entry in self._recognitions.values()]
        for task in recognition_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*recognition_tasks, return_exceptions=True)
        self._recognitions.clear()
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()
