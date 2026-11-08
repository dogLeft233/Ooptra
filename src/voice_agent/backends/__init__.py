from voice_agent.backends.base import VoiceBackend, VoiceReply

__all__ = ["VoiceBackend", "VoiceReply", "create_backend"]


def create_backend(settings, memory):
    backend = (settings.backend or "").strip().lower()
    if backend == "aliyun_deepseek":
        from voice_agent.backends.aliyun_deepseek import AliyunDeepSeekBackend

        return AliyunDeepSeekBackend(settings, memory)
    # Live 优先：端到端语音，不做「文-想-说」级联
    if backend in {"gemini", "gemini_live", "live", "live_gemini", "speech_to_speech"}:
        from voice_agent.backends.gemini_live import GeminiLiveBackend

        return GeminiLiveBackend(settings, memory)
    if backend in {"openai", "openai_realtime", "realtime"}:
        from voice_agent.backends.gemini_live import OpenAiRealtimeBackend

        return OpenAiRealtimeBackend(settings, memory)
    if backend in {"mimo", "mimo_cascade", "cascade", "cascade_mimo"}:
        from voice_agent.backends.mimo_cascade import MimoCascadeBackend

        return MimoCascadeBackend(settings, memory)
    raise ValueError(f"unknown voice backend: {settings.backend}")
