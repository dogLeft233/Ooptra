"""桥接生命周期：组装 SDK、OneBot v11 反向 WebSocket，并监督自动重启。

所有启动/停止都只由内部监督任务串行执行，外部（WebUI、信号处理）只投递请求，
避免停止与启动交错造成的竞态。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from channels._internal.adapters.bridge.onebot_server import BridgeOneBotV11Server
from channels._internal.adapters.bridge.shim import SdkBridgeShim
from channels._internal.adapters.bridge.state import BridgeState, describe_error
from channels._internal.adapters.onebot_v11.config import (
    OneBotV11ServerConfig as ProjectOneBotConfig,
)
from channels._internal.adapters.onebot_v11.config import get_onebot_v11_config
from channels._internal.adapters.onebot_v11.sdk_integration import (
    OneBotV11Supplement,
    find_sdk_onebot_v11,
)
from channels._internal.adapters.oopz.name_resolver import get_resolver
from channels._internal.adapters.oopz.proxy_transport import install_proxy_transports
from channels._internal.adapters.oopz.sdk_config import build_sdk_config
from channels.api import PlatformConfiguration, VoiceLifecycle
from core.logger_config import get_logger
from oopz_sdk import OopzBot
from oopz_sdk.adapters.onebot.v11 import OneBotV11ServerConfig as SdkOneBotServerConfig
from oopz_sdk.exceptions import OopzAuthError

logger = get_logger("Bridge")

DEFAULT_RETRY_INTERVAL = 30.0
STOP_TIMEOUT = 10.0


class BridgeController:
    """Oopz <-> OneBot v11 反向 WebSocket 桥接。"""

    def __init__(
        self,
        state: BridgeState,
        *,
        retry_interval: float = DEFAULT_RETRY_INTERVAL,
        configuration: PlatformConfiguration | None = None,
        voice: VoiceLifecycle | None = None,
        sdk_factory=None,
    ) -> None:
        self.state = state
        self.configuration = configuration
        self.voice = voice
        self._sdk_factory = sdk_factory or OopzBot
        self._retry_interval = max(5.0, float(retry_interval))

        self._supervisor: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self._restart_requested = False
        self._restart_reason = ""

        self._bot: OopzBot | None = None
        self._adapter: Any = None
        self._server: BridgeOneBotV11Server | None = None
        self._supplement: OneBotV11Supplement | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._identity_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._supervisor is not None and not self._supervisor.done():
            return
        self._closing = False
        self._wake.clear()
        self._supervisor = asyncio.create_task(self._supervise(), name="bridge-supervisor")
        await asyncio.sleep(0)

    async def stop(self) -> None:
        self._closing = True
        self._wake.set()
        supervisor = self._supervisor
        self._supervisor = None
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        await self._teardown()

    async def restart(self, reason: str = "") -> None:
        """请求监督任务重建连接（凭据变更、手动重启等）。"""
        self._restart_requested = True
        self._restart_reason = reason or "手动触发"
        self._wake.set()
        if self._supervisor is None or self._supervisor.done():
            await self.start()

    @property
    def running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    def snapshot(self) -> dict[str, Any]:
        state = self.state.snapshot()
        state["runtime"]["supervisor_alive"] = (
            self._supervisor is not None and not self._supervisor.done()
        )
        return state

    # ------------------------------------------------------------------
    # 监督任务
    # ------------------------------------------------------------------

    async def _supervise(self) -> None:
        while not self._closing:
            # 消费这一轮的通知；后续 await 期间到达的新通知留给下一轮。
            self._wake.clear()
            if self._restart_requested:
                self._restart_requested = False
                self.state.restarts += 1
                logger.info("重启桥接：%s", self._restart_reason)
                await self._teardown()

            if not await self._ensure_running():
                await self._wait_wake(self._retry_interval)
                continue

            await self._wait_run()

    async def _wait_wake(self, timeout: float | None) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)

    async def _wait_run(self) -> None:
        task = self._run_task
        if task is None:
            return
        waiter = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter

        if self._wake.is_set():
            return
        self._record_run_exit(task)
        await self._teardown()

    def _record_run_exit(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            message = "连接循环被取消"
            logger.warning("桥接连接循环被取消，准备重启")
        else:
            exc = task.exception()
            if exc is None:
                message = "连接循环已结束"
                logger.warning("桥接连接循环自行结束，准备重启")
            else:
                message = describe_error(exc)
                logger.error("桥接连接循环异常退出：%s", exc)
        self.state.last_error = message
        self.state.running = False

    async def _ensure_running(self) -> bool:
        if self._bot is not None and self._run_task is not None:
            return True
        try:
            await self._start_bot()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state.running = False
            self.state.last_error = describe_error(exc)
            logger.error("桥接启动失败，%.0f 秒后重试：%s", self._retry_interval, exc)
            if isinstance(exc, OopzAuthError):
                logger.error("Oopz 凭据不可用：请在 WebUI 的「Oopz 登录」页重新登录。")
            return False
        return True

    # ------------------------------------------------------------------
    # 组装与拆除
    # ------------------------------------------------------------------

    async def _start_bot(self) -> None:
        if self.configuration is None:
            raise RuntimeError("平台配置端口未装配")
        oopz, onebot, _headers = self.configuration.snapshot()
        onebot_cfg = get_onebot_v11_config(onebot)
        if not onebot_cfg.enabled:
            raise RuntimeError("ONEBOT_V11_CONFIG.enabled 为 False，桥接无法启动")

        sdk_config, proxy, _proxy_value = await build_sdk_config(configuration=self.configuration)
        bot = self._sdk_factory(
            sdk_config,
            on_ready=self._on_oopz_ready,
            on_close=self._on_oopz_close,
            on_reconnect=self._on_oopz_reconnect,
            on_error=self._on_oopz_error,
        )
        install_proxy_transports(bot, proxy)
        adapter = find_sdk_onebot_v11(bot)
        if adapter is None:
            raise RuntimeError("内置 SDK 未安装 OneBot v11 adapter")

        server = BridgeOneBotV11Server(adapter, self._server_config(onebot_cfg), self.state)
        bot.add_adapter(adapter, server=server)
        adapter.add_event_sink(self.state.count_event)

        shim = SdkBridgeShim(
            bot,
            default_area=str(oopz.get("default_area") or ""),
            default_channel=str(oopz.get("default_channel") or ""),
        )
        await get_resolver().bind_gateway(shim)
        supplement = OneBotV11Supplement(adapter, shim, onebot_cfg)
        supplement.start()

        self._bot = bot
        self._adapter = adapter
        self._server = server
        self._supplement = supplement
        self.state.self_id = int(adapter.self_id)
        self.state.self_uid = str(adapter.self_oopz_id)
        self.state.oopz.target = str(getattr(sdk_config, "ws_url", "") or "")
        self.state.last_error = ""
        self.state.running = True
        self._log_plan(onebot_cfg)

        try:
            if self.voice is not None:
                self.voice.bind_bot(bot)
        except Exception:
            logger.debug("bind voice runtime failed", exc_info=True)

        task = asyncio.create_task(bot.start(), name="oopz-bridge")
        self._run_task = task
        # 让「凭据无效 / 首次连接失败」在此刻就抛出来，而不是等监督循环下一轮。
        await asyncio.sleep(0)
        if task.done():
            try:
                await task
            finally:
                await self._teardown()

    async def _teardown(self) -> None:
        bot = self._bot
        run_task = self._run_task
        supplement = self._supplement
        identity_task = self._identity_task
        self._bot = None
        self._adapter = None
        self._server = None
        self._supplement = None
        self._run_task = None
        self._identity_task = None
        self.state.running = False

        if identity_task is not None and not identity_task.done():
            identity_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await identity_task

        if run_task is not None and not run_task.done():
            run_task.cancel()
            with contextlib.suppress(BaseException):
                await run_task

        if supplement is not None:
            with contextlib.suppress(Exception):
                await supplement.stop(timeout=3.0)

        if bot is not None:
            with contextlib.suppress(Exception):
                if self.voice is not None:
                    await self.voice.unbind_bot(bot)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(bot.stop(), timeout=STOP_TIMEOUT)

        with contextlib.suppress(Exception):
            await get_resolver().flush()

        self.state.reverse_ws.mark_disconnected()
        self.state.oopz.mark_disconnected()

    # ------------------------------------------------------------------
    # SDK 回调
    #
    # 签名由 oopz_sdk 的事件分发器决定：ready / reconnect 只收 ctx，
    # close / error 收 (ctx, event)。改错参数个数会让回调被静默吞掉。
    # ------------------------------------------------------------------

    async def _on_oopz_ready(self, _ctx: Any) -> None:
        self.state.oopz.mark_connected()
        if self.voice is not None:
            await self.voice.set_bot_ready(True)
        logger.info("已连接 Oopz 事件 WebSocket")
        if self._identity_task is None or self._identity_task.done():
            self._identity_task = asyncio.create_task(
                self._load_identity(),
                name="bridge-identity",
            )

    async def _on_oopz_reconnect(self, _ctx: Any) -> None:
        self.state.oopz.mark_attempt()
        if self.voice is not None:
            await self.voice.set_bot_ready(False)
        logger.warning("正在重连 Oopz 事件 WebSocket")

    async def _on_oopz_close(self, _ctx: Any, payload: Any) -> None:
        if self.voice is not None:
            await self.voice.set_bot_ready(False)
        data = payload if isinstance(payload, dict) else {}
        reason = str(data.get("reason") or data.get("error") or "连接已关闭")
        if data.get("reconnecting"):
            self.state.oopz.mark_disconnected(f"重连中：{reason}")
            logger.warning("Oopz 事件 WebSocket 断开，SDK 正在重连：%s", reason)
        else:
            self.state.oopz.mark_disconnected(reason)
            logger.error("Oopz 事件 WebSocket 断开：%s", reason)

    async def _on_oopz_error(self, _ctx: Any, error: Any) -> None:
        message = describe_error(error) if isinstance(error, BaseException) else str(error)
        self.state.last_error = message
        logger.error("Oopz 事件流错误：%s", error)

    async def _load_identity(self) -> None:
        bot = self._bot
        if bot is None:
            return
        try:
            detail = await bot.person.get_self_detail()
            name = str(getattr(detail, "name", "") or "")
            self.state.nickname = name
            if name and self.state.self_uid:
                get_resolver().set_user(self.state.self_uid, name)
        except Exception as exc:
            logger.debug("获取自身资料失败: %s", exc)
        try:
            areas = await bot.areas.get_joined_areas()
            self.state.joined_areas = len(list(areas or []))
        except Exception as exc:
            logger.debug("获取已加入域失败: %s", exc)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _server_config(cfg: ProjectOneBotConfig) -> SdkOneBotServerConfig:
        return SdkOneBotServerConfig(
            host=cfg.host,
            port=cfg.port,
            access_token=cfg.access_token,
            secret=cfg.secret,
            enable_http=cfg.enable_http,
            enable_ws=cfg.enable_ws,
            enable_http_post=cfg.enable_http_post,
            enable_ws_reverse=cfg.enable_ws_reverse,
            http_post_urls=list(cfg.http_post_urls),
            http_post_timeout=cfg.http_post_timeout,
            ws_reverse_url=cfg.ws_reverse_url,
            ws_reverse_api_url=cfg.ws_reverse_api_url,
            ws_reverse_event_url=cfg.ws_reverse_event_url,
            ws_reverse_reconnect_interval=cfg.ws_reverse_reconnect_interval,
            send_connect_event=cfg.send_connect_event,
        )

    def _log_plan(self, cfg: ProjectOneBotConfig) -> None:
        modes: list[str] = []
        if cfg.enable_ws_reverse:
            targets = [
                str(url).strip()
                for url in (cfg.ws_reverse_url, cfg.ws_reverse_api_url, cfg.ws_reverse_event_url)
                if str(url).strip()
            ]
            modes.append("反向 WS -> " + ", ".join(targets))
        if cfg.enable_http or cfg.enable_ws:
            modes.append(f"本地服务 {cfg.host}:{cfg.port} (http={cfg.enable_http} ws={cfg.enable_ws})")
        if cfg.enable_http_post:
            modes.append("HTTP POST 上报 " + ", ".join(cfg.http_post_urls))
        logger.info("OneBot v11 桥接模式：%s", "；".join(modes) or "未启用任何传输方式")

        if (cfg.enable_http or cfg.enable_ws) and cfg.host not in {"127.0.0.1", "localhost", "::1"}:
            logger.warning(
                "本地 OneBot 服务监听在 %s 且未启用 access_token 时会暴露给同网段；"
                "如非必要请在 config.py 里关闭 enable_http / enable_ws。",
                cfg.host,
            )


__all__ = ["BridgeController"]
