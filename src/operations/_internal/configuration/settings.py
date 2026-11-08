"""语音 Agent / VOICE_API 配置加载。config.py 缺字段时用默认值，不阻塞启动。"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from dotenv import dotenv_values

from core.paths import project_path
from operations.models import VoiceAgentSettings, VoiceApiSettings


def _get(cfg: dict[str, Any], key: str, default: Any) -> Any:
    value = cfg.get(key, default)
    return default if value is None else value


def _output_volume(value: Any) -> int:
    try:
        return max(10, min(100, int(str(value).strip())))
    except (TypeError, ValueError):
        return 50


def load_voice_agent_settings(*, runtime_config=None, environ=None, local_environment=None, local_environment_path=None) -> tuple[VoiceAgentSettings, VoiceApiSettings]:
    """显式加载配置；默认读取部署模块/环境，本函数导入时不读取凭据。"""
    if runtime_config is None:
        try:
            import config as runtime_config
        except Exception:
            runtime_config = None
    environment = os.environ if environ is None else environ

    def read_environment(name, default=""):
        value = environment.get(name)
        return default if value is None else value

    agent_raw = dict(getattr(runtime_config, "VOICE_AGENT_CONFIG", None) or {})
    api_raw = dict(getattr(runtime_config, "VOICE_API_CONFIG", None) or {})

    mimo = dict(_get(agent_raw, "mimo", {}) or {})
    gemini = dict(_get(agent_raw, "gemini", {}) or {})
    openai = dict(_get(agent_raw, "openai", {}) or {})
    cascade = dict(_get(agent_raw, "aliyun_deepseek", {}) or {})
    local = (dotenv_values(local_environment_path or project_path(".env.local"), interpolate=False)
             if local_environment is None else local_environment)
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
        mimo_api_key=read_environment("MIMO_API_KEY", str(mimo.get("api_key", "") or "")),
        mimo_base_url=str(mimo.get("base_url", VoiceAgentSettings.mimo_base_url) or VoiceAgentSettings.mimo_base_url),
        mimo_asr_model=str(mimo.get("asr_model", "mimo-v2.5-asr") or "mimo-v2.5-asr"),
        mimo_llm_model=str(mimo.get("llm_model", "mimo-v2.6-flash") or "mimo-v2.6-flash"),
        mimo_tts_model=str(mimo.get("tts_model", "mimo-v2.5-tts") or "mimo-v2.5-tts"),
        mimo_tts_voice=str(mimo.get("tts_voice", "冰糖") or "冰糖"),
        dashscope_api_key=read_environment("DASHSCOPE_API_KEY", local.get("DASHSCOPE_API_KEY") or ""),
        deepseek_api_key=read_environment("DEEPSEEK_API_KEY", local.get("DEEPSEEK_API_KEY") or ""),
        dashscope_ws_url=str(cascade.get("ws_url") or VoiceAgentSettings.dashscope_ws_url),
        aliyun_asr_model=str(cascade.get("asr_model") or VoiceAgentSettings.aliyun_asr_model),
        aliyun_tts_model=str(cascade.get("tts_model") or VoiceAgentSettings.aliyun_tts_model),
        aliyun_tts_voice=str(cascade.get("tts_voice") or VoiceAgentSettings.aliyun_tts_voice),
        deepseek_base_url=str(cascade.get("llm_base_url") or VoiceAgentSettings.deepseek_base_url),
        deepseek_model=str(cascade.get("llm_model") or VoiceAgentSettings.deepseek_model),
        gemini_api_key=read_environment("GEMINI_API_KEY", str(gemini.get("api_key", "") or "")),
        gemini_base_url=str(gemini.get("base_url", VoiceAgentSettings.gemini_base_url) or VoiceAgentSettings.gemini_base_url),
        gemini_model=str(gemini.get("model", "gemini-2.0-flash-live-001") or "gemini-2.0-flash-live-001"),
        gemini_voice=str(gemini.get("voice", "Puck") or "Puck"),
        openai_api_key=read_environment("OPENAI_API_KEY", str(openai.get("api_key", "") or "")),
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
    backend.settings`` 是同一个对象，改一处三处同时可见，不必重建 VoiceCoordinator。
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
