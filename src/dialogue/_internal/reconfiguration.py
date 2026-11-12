"""生成配置的连接失效规则；供应商字段只在生成模块维护。"""

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


def requires_session_restart(changed: set[str]) -> bool:
    """字段变更是否使现有供应商会话失效；调用者负责跨模块停止/重开。"""
    return bool(_SESSION_FIELDS & changed) or "backend" in changed
