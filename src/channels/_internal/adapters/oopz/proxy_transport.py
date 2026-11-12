"""把项目的代理设置装进 SDK 传输层。

SDK 自带的 ``HttpTransport`` / ``WebSocketTransport`` 直接 ``aiohttp.ClientSession(...)``，
而 aiohttp 的 ``trust_env`` 默认为 False：``HTTP_PROXY`` / ``ALL_PROXY`` 这类系统代理
环境变量不会生效，``proxy=`` 参数也只认 http(s) 代理。这里只替换「会话与连接器」的
装配，协议逻辑仍全部走 SDK 原实现。

- ``system`` 模式：``trust_env=True``，让 aiohttp 自己读系统代理环境变量；
- ``socks5`` 模式：用 aiohttp-socks 的 ProxyConnector（可选依赖）；
- 显式 http(s) 代理：仍由 ``OopzConfig.proxy`` 透传给 aiohttp。
"""

from __future__ import annotations

import importlib
from typing import Any

import aiohttp

from core.logger_config import get_logger
from core.proxy_utils import ProxySettings
from oopz_sdk.transport.http import HttpTransport
from oopz_sdk.transport.ws import WebSocketTransport

logger = get_logger("OopzTransport")


def _connector(settings: ProxySettings) -> aiohttp.BaseConnector | None:
    if not settings.is_socks:
        return None
    try:
        socks = importlib.import_module("aiohttp_socks")
    except ImportError as exc:  # 可选依赖缺失时给出可执行的提示
        raise RuntimeError(
            "OOPZ_CONFIG.proxy 配置了 socks5 代理，但缺少可选依赖："
            "pip install -r requirements-optional.txt"
        ) from exc
    return socks.ProxyConnector.from_url(str(settings.server))


def _new_session(headers: Any, settings: ProxySettings) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        headers=headers,
        connector=_connector(settings),
        trust_env=settings.mode == "system",
    )


class ProxyHttpTransport(HttpTransport):
    """REST / 上传请求的代理装配。"""

    def __init__(self, config, signer, *, auth_manager=None, proxy: ProxySettings):
        super().__init__(config, signer, auth_manager=auth_manager)
        self.project_proxy = proxy

    async def _ensure_client_session(self) -> aiohttp.ClientSession:
        if self._client_session is None or self._client_session.closed:
            self._client_session = _new_session(self.headers, self.project_proxy)
        return self._client_session


class ProxyWebSocketTransport(WebSocketTransport):
    """事件 WebSocket 的代理装配（其余行为沿用 SDK 实现）。"""

    def __init__(self, config, *, proxy: ProxySettings):
        super().__init__(config)
        self.project_proxy = proxy

    async def connect(self) -> None:
        # SDK 的 connect() 只在 session 为空时新建，先按项目代理建好再交给它复用。
        if self._session is None or self._session.closed:
            self._session = _new_session(self.config.get_headers(), self.project_proxy)
        await super().connect()


def install_proxy_transports(bot, proxy: ProxySettings) -> None:
    """在 ``bot.start()`` 之前替换各 service 共享的传输对象。"""
    logger.info("代理模式：%s", _describe(proxy))

    transport = ProxyHttpTransport(
        bot.config,
        bot.rest.signer,
        auth_manager=bot.auth,
        proxy=proxy,
    )
    bot.rest.transport = transport
    for service in (
        bot.messages,
        bot.media,
        bot.areas,
        bot.channels,
        bot.person,
        bot.moderation,
        bot.general,
        bot.voice,
    ):
        service.transport = transport

    bot.ws.transport = ProxyWebSocketTransport(bot.config, proxy=proxy)


def _describe(proxy: ProxySettings) -> str:
    if proxy.mode == "direct":
        return "直连（忽略系统代理）"
    if proxy.enabled:
        if proxy.username:
            return f"{proxy.scheme}://***@{proxy.host}:{proxy.port}"
        return str(proxy.server)
    return "系统代理环境变量（未设置则直连）"


__all__ = [
    "ProxyHttpTransport",
    "ProxyWebSocketTransport",
    "install_proxy_transports",
]
