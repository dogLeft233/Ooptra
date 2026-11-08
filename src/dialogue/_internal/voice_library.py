"""Qwen 音色复刻协议；云端是音色目录权威，当前选择由维护配置拥有。"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from urllib.parse import urlsplit

import aiohttp

from dialogue.api import DialogueSettings
from dialogue.models import ManagedVoice, VoicePage, VoiceSample

logger = logging.getLogger(__name__)


class VoiceLibraryError(ValueError):
    """只包含安全的阶段描述，不携带供应商正文或凭据。"""


def _text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > 200:
        raise VoiceLibraryError(f"音色服务返回无效的 {field}")
    return raw


def _voice_id(raw: object) -> str:
    value = _text(raw, "voice_id")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", value):
        raise ValueError("音色 ID 格式无效")
    return value


class QwenVoiceLibrary:
    def __init__(self, *, endpoint: str | None = None) -> None:
        self._endpoint = endpoint
        self._creating = False

    def _url(self, settings: DialogueSettings) -> str:
        if settings.backend != "aliyun_deepseek":
            raise ValueError("音色管理需要先将语音后端设置为「阿里语音 + DeepSeek」")
        if not settings.aliyun_tts_model.startswith("qwen-audio-"):
            raise ValueError("当前 TTS 模型不支持此音色管理接口")
        if not settings.dashscope_api_key:
            raise ValueError("请先配置 DASHSCOPE_API_KEY")
        if self._endpoint:
            return self._endpoint
        url = urlsplit(settings.dashscope_ws_url)
        if url.scheme != "wss" or not url.netloc or url.username or url.password:
            raise ValueError("请检查阿里语音 WebSocket 地址")
        return f"https://{url.netloc}/api/v1/services/audio/tts/customization"

    async def _call(self, settings: DialogueSettings, fields: dict[str, object]) -> dict:
        url = self._url(settings)
        from dialogue._internal.proxy import resolve_agent_proxy_url
        proxy = resolve_agent_proxy_url(settings.proxy or "")
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session,
                session.post(url, headers={"Authorization": f"Bearer {settings.dashscope_api_key}"},
                    json={"model": "voice-enrollment", "input": fields}, proxy=proxy,
                    allow_redirects=False) as response,
            ):
                if response.status != 200:
                    raise VoiceLibraryError(f"音色服务请求失败（HTTP {response.status}）")
                data = await response.json(content_type=None)
                if (not isinstance(data, dict) or data.get("code")
                        or not isinstance(data.get("output"), dict)):
                    raise VoiceLibraryError("音色服务返回失败或无效响应")
                return data["output"]
        except VoiceLibraryError:
            logger.warning("event=dialogue.voice_library.failed action=%s reason=provider", fields["action"])
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            logger.warning("event=dialogue.voice_library.failed action=%s reason=transport", fields["action"])
            raise VoiceLibraryError("音色服务连接失败或超时；创建请求请先刷新列表再重试") from None

    async def list(self, settings: DialogueSettings, page: int = 0) -> VoicePage:
        if type(page) is not int or not 0 <= page <= 10:
            raise ValueError("音色页码必须为 0到10")
        output = await self._call(settings, {"action": "list_voice", "page_index": page, "page_size": 100})
        rows = output.get("voice_list")
        if not isinstance(rows, list) or len(rows) > 100:
            raise VoiceLibraryError("音色服务返回无效列表")
        voices = []
        for row in rows:
            if not isinstance(row, dict):
                raise VoiceLibraryError("音色服务返回无效列表项")
            voice_id = _voice_id(row.get("voice_id"))
            # 此接口列表不含 target_model；官方 ID 格式以完整目标模型名开头。
            if voice_id.startswith(settings.aliyun_tts_model + "-"):
                voices.append(ManagedVoice(voice_id, settings.aliyun_tts_model,
                                           _text(row.get("status"), "status")))
        return VoicePage(tuple(voices), page, len(rows) == 100 and page < 10)

    async def check(self, settings: DialogueSettings, voice_id: str) -> ManagedVoice:
        voice_id = _voice_id(voice_id)
        if voice_id == "longanhuan_v3.1" and settings.aliyun_tts_model == "qwen-audio-3.1-tts-flash":
            self._url(settings)
            return ManagedVoice(voice_id, settings.aliyun_tts_model, "OK", "system")
        output = await self._call(settings, {"action": "query_voice", "voice_id": voice_id})
        target = _text(output.get("target_model"), "target_model")
        if target != settings.aliyun_tts_model:
            raise ValueError("此音色绑定的模型与当前 TTS 模型不同")
        return ManagedVoice(voice_id, target, _text(output.get("status"), "status"))

    async def create(self, settings: DialogueSettings, sample: VoiceSample) -> ManagedVoice:
        if self._creating:
            raise ValueError("正在创建音色，请等待完成后刷新列表")
        self._creating = True
        try:
            audio = base64.b64encode(sample.audio).decode("ascii")
            output = await self._call(settings, {"action": "create_voice",
                "target_model": settings.aliyun_tts_model, "prefix": sample.prefix,
                "audio": f"data:{sample.mime_type};base64,{audio}"})
            voice_id = _voice_id(output.get("voice_id"))
            # 创建成功即返回 ID；部署状态由后续列表/详情查询确认，避免查询失败丢失新 ID。
            return ManagedVoice(voice_id, settings.aliyun_tts_model, "DEPLOYING")
        finally:
            self._creating = False
