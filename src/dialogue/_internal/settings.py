"""生成配置捕获与持续模型会话失效规则；不拥有维护版本或渠道流程。"""
from __future__ import annotations

from dialogue.api import DialogueSettings, VoiceBackend
from dialogue.models import SettingsUpdate

_LIVE_SESSION_FIELDS = {
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


def update_settings(backend: VoiceBackend, settings: DialogueSettings,
                    changed: tuple[str, ...]) -> SettingsUpdate:
    backend.settings = settings
    fields = set(changed)
    deferred = frozenset(fields & {"deepseek_reasoning_effort"})
    notes = ("思考强度已更新，从下一次生成回复生效",) if deferred else ()
    return SettingsUpdate(deferred, backend.capabilities().live and bool(fields & _LIVE_SESSION_FIELDS), notes)
