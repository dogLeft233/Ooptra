"""VOICE_API：供 AstrBot 插件 / 控制台调用的本地 HTTP。

本模块**只负责监听与鉴权**，路由实现全部来自 ``webui.voice_routes``——
3090（WebUI）与 3091（本端口）必须是同一套 handler，否则会像过去那样分叉：
3091 曾经自带一份手写路由，缺 ``/voice/channels``、``/voice/members`` 还会 500。

持有 ``VoiceRuntime`` 而不是 ``agent``：handler 每次请求重新读
``runtime.agent``，热重载替换 agent 后无需重启端口。
"""

from __future__ import annotations

import hmac
import ipaddress
import logging

from aiohttp import web

from operations.models import VoiceApiSettings
from webui.voice_routes import build_voice_routes

logger = logging.getLogger(__name__)

_WILDCARD_HOSTS = {"", "*", "0.0.0.0", "::", "[::]"}


def _is_loopback(host: str) -> bool:
    text = str(host or "").strip().lower()
    if text in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _same_bind_target(host_a: object, port_a: object, host_b: object, port_b: object) -> bool:
    """两个监听地址是否会互相抢流量（同端口，且主机范围有交集）。"""
    try:
        if int(port_a) != int(port_b):  # type: ignore[arg-type]
            return False
    except (TypeError, ValueError):
        return False
    a = str(host_a or "").strip().lower()
    b = str(host_b or "").strip().lower()
    if a in _WILDCARD_HOSTS or b in _WILDCARD_HOSTS:
        return True
    if a == b:
        return True
    return _is_loopback(a) and _is_loopback(b)


class VoiceApiServer:
    def __init__(self, runtime, settings: VoiceApiSettings) -> None:
        self.runtime = runtime
        self.settings = settings
        self._runner: web.AppRunner | None = None
        self._webui_endpoint: tuple[str, int] | None = None

    def set_webui_endpoint(self, host: str, port: int) -> None:
        """告知 Web 控制台**实际**绑定的地址，用于避免两个 app 抢同一个端口。"""
        self._webui_endpoint = (str(host), int(port))

    def _authed(self, request: web.Request) -> bool:
        token = (self.settings.token or "").strip()
        if not token:
            return False
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer ") and hmac.compare_digest(header[7:].strip().encode(), token.encode()):
            return True
        if hmac.compare_digest(request.query.get("token", "").encode(), token.encode()):
            return True
        return hmac.compare_digest(request.headers.get("X-Ooptra-Token", "").encode(), token.encode())

    def _err(self, message: str, status: int = 400) -> web.Response:
        return web.json_response({"ok": False, "error": message}, status=status)

    async def _guard(self, request: web.Request, handler) -> web.Response:
        if not self._authed(request):
            return self._err("访问令牌无效", 401)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as exc:
            logger.exception("voice api handler failed")
            return self._err(str(exc), 500)

    def _wrap(self, handler):
        """把 handler 包成带鉴权与异常兜底的 aiohttp handler。"""

        async def wrapped(request: web.Request) -> web.Response:
            return await self._guard(request, handler)

        return wrapped

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self.settings.enabled:
            logger.info("voice api disabled")
            return
        webui = self._webui_endpoint
        if webui is not None and _same_bind_target(
            self.settings.host, self.settings.port, webui[0], webui[1]
        ):
            # Windows 上 aiohttp 默认 SO_REUSEADDR，0.0.0.0:3090 与 127.0.0.1:3090 能
            # 同时绑成功，但更具体的 127.0.0.1 会**抢走**本机回环流量 —— WebUI 自己的
            # 页面和 /api/* 在 127.0.0.1 上全部 404（2026-09-29 实测踩过）。直接不启动。
            logger.warning(
                "VOICE_API 的 %s:%s 与 Web 控制台（%s:%s）是同一监听地址，已跳过启动："
                "2.0.0 起两者路由完全一致，WebUI 端口本身就是完整语音 API；"
                "确实需要独立端口，请把 VOICE_API_CONFIG.port 改成别的值",
                self.settings.host,
                self.settings.port,
                webui[0],
                webui[1],
            )
            return
        app = web.Application(client_max_size=3 * 1024 * 1024)
        app.add_routes(build_voice_routes(self.runtime, wrap=self._wrap))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.settings.host, self.settings.port)
        await site.start()
        self._runner = runner
        if not (self.settings.token or "").strip():
            logger.warning(
                "VOICE_API on %s:%s has no shared password configured; requests are rejected "
                "until WEBUI_CONFIG.token is set",
                self.settings.host,
                self.settings.port,
            )
        logger.info(
            "VOICE_API listening on http://%s:%s",
            self.settings.host,
            self.settings.port,
        )

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        if runner is not None:
            await runner.cleanup()
