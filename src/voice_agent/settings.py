"""语音 Agent / VOICE_API 配置加载。config.py 缺字段时用默认值，不阻塞启动。"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import dotenv_values

from core.paths import project_path


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value


def _get(cfg: dict[str, Any], key: str, default: Any) -> Any:
    value = cfg.get(key, default)
    return default if value is None else value


def _output_volume(value: Any) -> int:
    try:
        return max(10, min(100, int(str(value).strip())))
    except (TypeError, ValueError):
        return 50


@dataclass
class VoiceAgentSettings:
    enabled: bool = False
    backend: str = "gemini_live"
    persona: str = "你是 Oopz 语音频道助手，说话简洁友好，像在语音房里自然聊天。"
    area: str = ""
    channel: str = ""
    barge_in: bool = True
    #: 抢话门限：房间里某人要**连续说话**满这么多毫秒，才认作「真的要打断 bot」。
    #: 0 = 关闭门限（退回旧行为：第一帧就打断，热闹房间里 bot 说不完一句话）。
    barge_in_hold_ms: int = 300
    vad_mode: str = "local"
    listen_only_uids: list[str] = field(default_factory=list)
    reply_text_to_channel: bool = False
    # 自动接话概率；快捷开口与进退房台词始终可用。
    reply_probability_percent: int = 30
    force_reply_keywords: list[str] = field(default_factory=list)
    conversation_window_enabled: bool = True
    conversation_window_seconds: int = 30
    voice_leave_enabled: bool = True
    voice_volume_enabled: bool = True
    sample_rate_in: int = 16000
    sample_rate_out: int = 24000
    output_volume: int = 50
    #: 说完多久算一句。700ms 对中文太短 —— 想词、换气的自然停顿就会被当成
    #: 「说完了」，一句话被切成几段，bot 逐段回（表现为「我每句话必回」）。
    silence_ms: int = 1200
    max_utterance_ms: int = 15000
    #: 短于这么多毫秒的音频不送 ASR。实测「对。」0.1s、「哦。」0.4s 这类一个字
    #: 的气声，回了也只是噪音。``_MIN_UTTERANCE_RMS`` 按**能量**过滤，管不到它们。
    min_utterance_ms: int = 500
    #: 回合节流：一条回合**完整说完**之后，至少隔这么多毫秒才允许开始下一回合。
    #: 不节流的话 bot 一直在出声，用户说下一句时它还没说完 —— 队列只增不减，
    #: 体感就是「延迟越来越高」。被人抢话打断的回合不计冷却（人家要的是立刻回应）。
    #: 0 = 不冷却。
    reply_cooldown_ms: int = 2500
    # 每次进房的级联自动接话尝试上限；0 不限制，失败也消耗额度。
    cascade_turn_limit: int = 0

    # 模型 API 网络出口：""=直连/系统，"clash"=127.0.0.1:7890，或显式 URL
    proxy: str = ""

    # MiMo 级联
    mimo_api_key: str = ""
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_asr_model: str = "mimo-v2.5-asr"
    mimo_llm_model: str = "mimo-v2.6-flash"
    mimo_tts_model: str = "mimo-v2.5-tts"
    mimo_tts_voice: str = "冰糖"

    # Aliyun ASR/TTS + DeepSeek. Keys never enter the WebUI configuration schema.
    dashscope_api_key: str = field(default="", repr=False)
    dashscope_ws_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    aliyun_asr_model: str = "qwen-audio-3.1-asr-flash-message"
    aliyun_tts_model: str = "qwen-audio-3.1-tts-flash"
    aliyun_tts_voice: str = "longanhuan_v3.1"
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"

    # Gemini Live
    gemini_api_key: str = ""
    gemini_base_url: str = (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    )
    gemini_model: str = "gemini-2.0-flash-live-001"
    gemini_voice: str = "Puck"

    # OpenAI Realtime（骨架）
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_realtime_model: str = "gpt-4o-mini-realtime-preview"
    openai_voice: str = "alloy"

    memory_path: str = "data/voice_memory.jsonl"
    memory_max_turns: int = 30


@dataclass
class VoiceApiSettings:
    # 独立端口默认关闭：语音接口已合并到 WebUI /api/*
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 3091
    token: str = ""


def load_voice_agent_settings() -> tuple[VoiceAgentSettings, VoiceApiSettings]:
    try:
        import config as runtime_config
    except Exception:
        runtime_config = None

    agent_raw = dict(getattr(runtime_config, "VOICE_AGENT_CONFIG", None) or {})
    api_raw = dict(getattr(runtime_config, "VOICE_API_CONFIG", None) or {})

    mimo = dict(_get(agent_raw, "mimo", {}) or {})
    gemini = dict(_get(agent_raw, "gemini", {}) or {})
    openai = dict(_get(agent_raw, "openai", {}) or {})
    cascade = dict(_get(agent_raw, "aliyun_deepseek", {}) or {})
    local = dotenv_values(project_path(".env.local"), interpolate=False)
    keywords = _get(agent_raw, "force_reply_keywords", []) or []
    if isinstance(keywords, str):
        keywords = keywords.split(",")

    agent = VoiceAgentSettings(
        enabled=bool(_get(agent_raw, "enabled", False)),
        backend=str(_get(agent_raw, "backend", "gemini_live") or "gemini_live"),
        persona=str(_get(agent_raw, "persona", VoiceAgentSettings.persona)),
        area=str(_get(agent_raw, "area", "") or ""),
        channel=str(_get(agent_raw, "channel", "") or ""),
        barge_in=bool(_get(agent_raw, "barge_in", True)),
        barge_in_hold_ms=int(_get(agent_raw, "barge_in_hold_ms", 300)),
        vad_mode=str(_get(agent_raw, "vad_mode", "local") or "local"),
        listen_only_uids=[str(x) for x in (_get(agent_raw, "listen_only_uids", []) or [])],
        reply_text_to_channel=bool(_get(agent_raw, "reply_text_to_channel", False)),
        reply_probability_percent=max(0, min(100, int(_get(agent_raw, "reply_probability_percent", 30)))),
        force_reply_keywords=[str(word).strip() for word in keywords if str(word).strip()],
        conversation_window_enabled=bool(_get(agent_raw, "conversation_window_enabled", True)),
        conversation_window_seconds=max(1, min(300, int(_get(agent_raw, "conversation_window_seconds", 30)))),
        voice_leave_enabled=bool(_get(agent_raw, "voice_leave_enabled", True)),
        voice_volume_enabled=bool(_get(agent_raw, "voice_volume_enabled", True)),
        sample_rate_in=int(_get(agent_raw, "sample_rate_in", 16000)),
        sample_rate_out=int(_get(agent_raw, "sample_rate_out", 24000)),
        output_volume=_output_volume(_get(agent_raw, "output_volume", 50)),
        silence_ms=int(_get(agent_raw, "silence_ms", 1200)),
        max_utterance_ms=int(_get(agent_raw, "max_utterance_ms", 15000)),
        min_utterance_ms=int(_get(agent_raw, "min_utterance_ms", 500)),
        reply_cooldown_ms=int(_get(agent_raw, "reply_cooldown_ms", 2500)),
        cascade_turn_limit=max(0, int(_get(agent_raw, "cascade_turn_limit", 0))),
        proxy=str(_get(agent_raw, "proxy", "") or ""),
        mimo_api_key=_env("MIMO_API_KEY", str(mimo.get("api_key", "") or "")),
        mimo_base_url=str(mimo.get("base_url", VoiceAgentSettings.mimo_base_url) or VoiceAgentSettings.mimo_base_url),
        mimo_asr_model=str(mimo.get("asr_model", "mimo-v2.5-asr") or "mimo-v2.5-asr"),
        mimo_llm_model=str(mimo.get("llm_model", "mimo-v2.6-flash") or "mimo-v2.6-flash"),
        mimo_tts_model=str(mimo.get("tts_model", "mimo-v2.5-tts") or "mimo-v2.5-tts"),
        mimo_tts_voice=str(mimo.get("tts_voice", "冰糖") or "冰糖"),
        dashscope_api_key=_env("DASHSCOPE_API_KEY", local.get("DASHSCOPE_API_KEY") or ""),
        deepseek_api_key=_env("DEEPSEEK_API_KEY", local.get("DEEPSEEK_API_KEY") or ""),
        dashscope_ws_url=str(cascade.get("ws_url") or VoiceAgentSettings.dashscope_ws_url),
        aliyun_asr_model=str(cascade.get("asr_model") or VoiceAgentSettings.aliyun_asr_model),
        aliyun_tts_model=str(cascade.get("tts_model") or VoiceAgentSettings.aliyun_tts_model),
        aliyun_tts_voice=str(cascade.get("tts_voice") or VoiceAgentSettings.aliyun_tts_voice),
        deepseek_base_url=str(cascade.get("llm_base_url") or VoiceAgentSettings.deepseek_base_url),
        deepseek_model=str(cascade.get("llm_model") or VoiceAgentSettings.deepseek_model),
        gemini_api_key=_env("GEMINI_API_KEY", str(gemini.get("api_key", "") or "")),
        gemini_base_url=str(gemini.get("base_url", VoiceAgentSettings.gemini_base_url) or VoiceAgentSettings.gemini_base_url),
        gemini_model=str(gemini.get("model", "gemini-2.0-flash-live-001") or "gemini-2.0-flash-live-001"),
        gemini_voice=str(gemini.get("voice", "Puck") or "Puck"),
        openai_api_key=_env("OPENAI_API_KEY", str(openai.get("api_key", "") or "")),
        openai_base_url=str(openai.get("base_url", VoiceAgentSettings.openai_base_url) or VoiceAgentSettings.openai_base_url),
        openai_realtime_model=str(
            openai.get("realtime_model", "gpt-4o-mini-realtime-preview") or "gpt-4o-mini-realtime-preview"
        ),
        openai_voice=str(openai.get("voice", "alloy") or "alloy"),
        memory_path=str(_get(agent_raw, "memory_path", "data/voice_memory.jsonl") or "data/voice_memory.jsonl"),
        memory_max_turns=int(_get(agent_raw, "memory_max_turns", 30)),
    )

    api = VoiceApiSettings(
        enabled=bool(_get(api_raw, "enabled", False)),
        host=str(_get(api_raw, "host", "127.0.0.1") or "127.0.0.1"),
        port=int(_get(api_raw, "port", 3091)),
        token=str((getattr(runtime_config, "WEBUI_CONFIG", None) or {}).get("token", "") or "").strip(),
    )
    return agent, api


def apply_settings(target: Any, fresh: Any) -> list[str]:
    """把 ``fresh`` 的字段**就地**写回 ``target``，返回变化的字段名。

    就地写回是刻意的：``runtime.agent_settings is agent.settings is
    backend.settings`` 是同一个对象，改一处三处同时可见，不必重建 VoiceAgent。
    """
    changed: list[str] = []
    for item in dataclasses.fields(target):
        new = getattr(fresh, item.name, None)
        if getattr(target, item.name, None) != new:
            setattr(target, item.name, new)
            changed.append(item.name)
    return changed


def resolve_agent_proxy_url(proxy: str) -> str | None:
    """把 VOICE_AGENT_CONFIG.proxy 解析成 aiohttp/websockets 可用的代理 URL。

    返回 ``None`` 表示不指定代理（交给系统/库默认行为）。
    """
    from core.proxy_utils import resolve_proxy_settings

    settings = resolve_proxy_settings(proxy)
    if settings.mode != "explicit" or not settings.server:
        return None
    return str(settings.server)
