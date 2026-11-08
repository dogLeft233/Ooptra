"""Live 会话 WebSocket 连接：无代理走 websockets，有代理走 aiohttp。

``websockets<14`` 原生不支持 HTTP 代理；``aiohttp`` 是硬依赖且 ``ws_connect``
支持 ``proxy=``，因此代理场景统一换成 aiohttp，并对外暴露与 websockets
兼容的 ``send`` / ``__aiter__`` / ``close`` 最小接口。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)


class _AiohttpWs:
    """把 aiohttp 的 ClientWebSocketResponse 适配成 websockets 风格。"""

    def __init__(self, ws: Any, session: Any) -> None:
        self._ws = ws
        self._session = session

    async def send(self, data: str) -> None:
        await self._ws.send_str(data)

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[str | bytes]:
        try:
            import aiohttp
        except ModuleNotFoundError:  # pragma: no cover
            aiohttp = None
        async for msg in self._ws:
            if aiohttp is not None and msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                # 这里以前是**静默** break：关闭码与异常全丢，日志里只剩一句
                # 「Gemini Live session ended」，排查断线原因时完全没有线索。
                # 仍不抛异常（上层靠迭代自然结束来判断断开），但要把原因记下来。
                exc = None
                with contextlib.suppress(Exception):
                    exc = self._ws.exception()
                logger.info(
                    "Live WebSocket 关闭：type=%s close_code=%s exception_type=%s",
                    msg.type.name,
                    getattr(self._ws, "close_code", None),
                    type(exc).__name__ if exc else None,
                )
                break
            data = getattr(msg, "data", msg)
            if isinstance(data, (bytes, str)):
                yield data

    async def close(self) -> None:
        try:
            await self._ws.close()
        finally:
            if self._session is not None:
                await self._session.close()


async def open_live_ws(
    url: str,
    *,
    proxy: str | None = None,
    max_size: int = 8 * 1024 * 1024,
) -> Any:
    """建立 Live WebSocket 连接。``proxy`` 为空则直连。"""
    if proxy:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30)
        session = aiohttp.ClientSession(timeout=timeout)
        try:
            ws = await session.ws_connect(
                url,
                proxy=proxy,
                max_msg_size=max_size,
                heartbeat=20,
            )
        except (Exception, asyncio.CancelledError):
            await session.close()
            raise
        return _AiohttpWs(ws, session)

    try:
        websockets = importlib.import_module("websockets")
    except ModuleNotFoundError as exc:
        raise RuntimeError("Live 后端需要 websockets：pip install websockets") from exc

    return await websockets.connect(
        url,
        max_size=max_size,
        ping_interval=20,
        ping_timeout=20,
    )


__all__ = ["open_live_ws"]
