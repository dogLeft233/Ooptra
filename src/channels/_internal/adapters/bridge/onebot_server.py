"""带状态上报的 OneBot v11 server。

纯桥接只使用反向 WebSocket：本地 HTTP / 正向 WS 是否开放由 config.py 决定，
默认全部关闭。``_connect_reverse_ws`` 与 SDK 实现保持一致，只额外上报连接状态。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from aiohttp import WSMsgType

from channels._internal.adapters.bridge.state import BridgeState, describe_error
from oopz_sdk.adapters.onebot.v11 import OneBotV11Server
from oopz_sdk.adapters.onebot.v11.server import WsRole

logger = logging.getLogger("bridge.onebot")


class _TextWebSocket(Protocol):
    async def send_str(self, data: str) -> None: ...


class BridgeOneBotV11Server(OneBotV11Server):
    """在 SDK server 之上补一层连接状态与流量计数。"""

    def __init__(self, adapter: Any, config: Any, state: BridgeState) -> None:
        super().__init__(adapter, config)
        self._state = state

    async def start(self) -> None:
        targets = [url for url, _role in self._reverse_targets()]
        if targets:
            self._state.reverse_ws.target = targets[0]
            if len(targets) > 1:
                logger.warning(
                    "配置了 %d 个反向 WebSocket 目标，状态面板只显示第一个: %s",
                    len(targets),
                    targets,
                )
        await super().start()

    # ------------------------------------------------------------------
    # 反向 WebSocket
    # ------------------------------------------------------------------

    async def _connect_reverse_ws(self, url: str, role: WsRole) -> None:
        link = self._state.reverse_ws
        link.mark_attempt(url)
        if self._session is None:
            raise RuntimeError("ClientSession is not initialized")

        logger.info("正在连接反向 WebSocket: url=%s role=%s", url, role)
        error_text = ""
        try:
            async with self._session.ws_connect(
                url,
                headers=self._reverse_ws_headers(role),
                heartbeat=120,
            ) as ws:
                link.mark_connected(url)
                logger.info("反向 WebSocket 已连接: url=%s role=%s", url, role)

                async def reverse_sink(event: dict[str, Any]) -> None:
                    if role in {"event", "universal"}:
                        await self._ws_send_json(ws, event)

                self.adapter.add_event_sink(reverse_sink)
                try:
                    if self.config.send_connect_event and role in {"event", "universal"}:
                        await self._ws_send_json(ws, self._connect_event())

                    async for msg in ws:
                        if role == "event":
                            continue
                        if msg.type == WSMsgType.TEXT:
                            await self._ws_send_json(
                                ws,
                                await self._handle_ws_payload_text(msg.data),
                            )
                        elif msg.type == WSMsgType.BINARY:
                            try:
                                text = msg.data.decode("utf-8")
                            except UnicodeDecodeError:
                                await self._ws_send_json(
                                    ws,
                                    self._failed(1400, "binary payload must be utf-8 json"),
                                )
                                continue
                            await self._ws_send_json(
                                ws,
                                await self._handle_ws_payload_text(text),
                            )
                        elif msg.type == WSMsgType.ERROR:
                            error_text = f"WebSocket 错误: {ws.exception()}"
                            logger.warning("反向 WebSocket 出错: %s", ws.exception())
                            break
                finally:
                    self.adapter.remove_event_sink(reverse_sink)
                    logger.info("反向 WebSocket 已断开: url=%s role=%s", url, role)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_text = describe_error(exc)
            raise
        finally:
            link.mark_disconnected(error_text)

    @staticmethod
    async def _ws_send_json(ws: _TextWebSocket, data: Any) -> None:
        # 正向与反向连接共享相同的 JSON 帧格式。
        await ws.send_str(json.dumps(data, ensure_ascii=False))

    # ------------------------------------------------------------------
    # action 计数
    # ------------------------------------------------------------------

    async def _handle_ws_payload_text(self, text: str) -> dict[str, Any]:
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict) and payload.get("action"):
            self._state.count_action(str(payload.get("action") or ""), payload)
        return await super()._handle_ws_payload_text(text)


__all__ = ["BridgeOneBotV11Server"]
