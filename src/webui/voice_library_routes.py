"""音色管理 HTTP 适配；选择保存复用 /api/config 的 CAS 和热应用回执。"""
from __future__ import annotations

import copy
from dataclasses import asdict
from pathlib import PurePath
from typing import Any

from aiohttp import BodyPartReader, web

from dialogue import create_voice_library
from dialogue.api import VoiceLibrary
from dialogue.models import VoiceSample


def voice_library_handlers(runtime: Any, library: VoiceLibrary | None = None) -> list[tuple]:
    service = library or create_voice_library()

    async def listing(request: web.Request) -> web.Response:
        try:
            settings = copy.deepcopy(runtime.agent.settings)
            page = int(request.query.get("page", "0"))
            result = await service.list(settings, page)
            return web.json_response({"ok": True, "voices": [asdict(v) for v in result.voices],
                "page": result.page, "has_more": result.has_more,
                "current_voice": settings.aliyun_tts_voice, "model": settings.aliyun_tts_model})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def create(request: web.Request) -> web.Response:
        try:
            # 流式读取 multipart，避免扩大其他 JSON 入口的请求体上限。
            if request.content_type != "multipart/form-data":
                raise ValueError("请以表单上传音频")
            reader = await request.multipart()
            fields: dict[str, bytes] = {}
            mime = ""
            total = 0
            while (part := await reader.next()) is not None:
                if not isinstance(part, BodyPartReader):
                    raise ValueError("不支持嵌套上传表单")
                if part.name not in {"prefix", "audio"} or part.name in fields:
                    raise ValueError("表单仅允许一个音色名称和一个音频文件")
                content = bytearray()
                while chunk := await part.read_chunk():
                    total += len(chunk)
                    content.extend(chunk)
                    if total > 10 * 1024 * 1024 + 10 or (part.name == "prefix" and len(content) > 10):
                        raise ValueError("参考音频不得超过 10 MB，名称不得超过 10 字符")
                fields[part.name] = bytes(content)
                if part.name == "audio":
                    mime = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}.get(
                        PurePath(part.filename or "").suffix.lower(), "")
            sample = VoiceSample(fields.get("prefix", b"").decode("ascii"), mime, fields.get("audio", b""))
            result = await service.create(copy.deepcopy(runtime.agent.settings), sample)
            return web.json_response({"ok": True, "voice": asdict(result)})
        except (ValueError, UnicodeError) as exc:
            message = "音色名称须为英文字母或数字" if isinstance(exc, UnicodeError) else str(exc)
            return web.json_response({"ok": False, "error": message}, status=400)

    async def check(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"voice_id"} or not isinstance(body["voice_id"], str):
                raise ValueError("请提供音色 ID")
            result = await service.check(copy.deepcopy(runtime.agent.settings), body["voice_id"])
            if result.status != "OK":
                raise ValueError("音色尚未就绪，请稍后刷新列表")
            return web.json_response({"ok": True, "voice": asdict(result)})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    return [("GET", "/voice/voices", listing), ("POST", "/voice/voices", create),
            ("POST", "/voice/voices/check", check)]
