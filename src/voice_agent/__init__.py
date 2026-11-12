"""Ooptra 语音对话 Agent：进房听人说话、调用模型、开口回话。"""

from operations import load_voice_agent_settings
from operations.models import VoiceAgentSettings

__all__ = ["VoiceAgentSettings", "load_voice_agent_settings"]
