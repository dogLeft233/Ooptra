"""Ooptra Web 控制台：状态、日志、配置与 Oopz 登录。

只依赖 aiohttp（桥接本身已有依赖），默认绑定 127.0.0.1:3090。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import ipaddress
import os
import platform
import sys
from typing import Any

from aiohttp import web

import config as runtime_config
from core.logger_config import get_logger
from core.paths import LOGS_DIR, PROJECT_ROOT
from core.version import __version__
from webui import config_editor
from webui.auth import AUTH_PATHS, ConsoleAuth
from webui.log_tail import LogTailer
from webui.oopz_login import OopzLoginService, credentials_summary

logger = get_logger("WebUI")

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3090
DEFAULT_LOG_LINES = 300
PAGE_PATHS = ("/", "/overview", "/logs", "/voice", "/maintenance", "/config", "/account")


def _is_loopback(host: str) -> bool:
    text = str(host or "").strip().lower()
    if text in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


class WebUIConsole:
    """控制台生命周期与 HTTP 接口。"""

    def __init__(
        self,
        state: Any,
        controller: Any,
        *,
        config: dict[str, Any] | None = None,
        voice_runtime: Any = None,
        shutdown: Any = None,
    ) -> None:
        self._state = state
        self._controller = controller
        self._config = config if isinstance(config, dict) else getattr(runtime_config, "WEBUI_CONFIG", {}) or {}
        self._voice_runtime = voice_runtime
        self._shutdown = shutdown
        self._bootstrap_ready = False
        self._maintenance = None
        self._tailer = LogTailer(LOGS_DIR)
        self._login = OopzLoginService(controller, state)
        self._token = ""
        self._auth = ConsoleAuth(lambda: self._token, self._setup_password)
        self._host = DEFAULT_HOST
        self._port = DEFAULT_PORT
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._configuration_tasks: set[asyncio.Task] = set()
        self._configuration_lock = asyncio.Lock()
        self._closing = False
        self._stop_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def bootstrap_ready(self) -> bool:
        return self._bootstrap_ready

    def set_bootstrap_ready(self, ready: bool) -> None:
        """The entrypoint marks local initialization complete, independently of remote links."""
        self._bootstrap_ready = bool(ready)

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def bind_address(self) -> tuple[str, int] | None:
        """实际绑定的 (host, port)；未启用或端口被占导致启动失败时为 None。"""
        if self._site is None:
            return None
        return (self._host, self._port)

    async def start(self) -> None:
        self._closing = False
        self._stop_task = None
        if not bool(self._config.get("enabled", True)):
            logger.info("Web 控制台已按 config.py 的 WEBUI_CONFIG.enabled 禁用")
            return

        self._host = str(self._config.get("host") or DEFAULT_HOST).strip() or DEFAULT_HOST
        self._port = int(self._config.get("port") or DEFAULT_PORT)
        self._token = str(self._config.get("token") or "").strip()

        if not self._token and not _is_loopback(self._host):
            logger.warning(
                "Web 控制台尚未设置密码，请从部署机器本机打开 %s 设置首次密码",
                self._host,
            )

        app = web.Application(middlewares=[self._auth_middleware], client_max_size=3 * 1024 * 1024)
        self._auth.mount(app)
        from operations import create_maintenance
        from webui.maintenance import mount_maintenance_routes

        self._maintenance = create_maintenance(PROJECT_ROOT, shutdown=self._shutdown,
                                               port=self._port, host=self._host, token=self._token,
                                               runtime_status=self._controller.snapshot,
                                               bootstrap_ready=lambda: self.bootstrap_ready)
        mount_maintenance_routes(app, self._maintenance)
        app.add_routes([web.get(path, self._handle_index) for path in PAGE_PATHS])
        app.add_routes(
            [
                web.get("/favicon.ico", self._handle_favicon),
                web.get("/assets/{name}", self._handle_asset),
                web.get("/api/status", self._handle_status),
                web.get("/api/credentials", self._handle_credentials),
                web.get("/api/update", self._handle_update_check),
                web.get("/api/logs", self._handle_logs_list),
                web.get("/api/logs/tail", self._handle_logs_tail),
                web.get("/api/logs/stream", self._handle_logs_stream),
                web.get("/api/config", self._handle_config_get),
                web.post("/api/config", self._handle_config_post),
                web.get("/api/login/browser", self._handle_browser_login_status),
                web.post("/api/login/browser", self._handle_browser_login_start),
                web.post("/api/login/browser/cancel", self._handle_browser_login_cancel),
                web.post("/api/login/api", self._handle_api_login),
                web.post("/api/bridge/restart", self._handle_bridge_restart),
            ]
        )
        if self._voice_runtime is not None:
            try:
                from webui.voice_routes import mount_voice_routes

                mount_voice_routes(app, self._voice_runtime)
            except Exception:
                logger.exception("挂载语音路由失败")

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host=self._host, port=self._port)
        try:
            await site.start()
        except OSError as exc:
            await runner.cleanup()
            logger.error(
                "Web 控制台启动失败（%s:%s）：%s；桥接不受影响，可修改 WEBUI_CONFIG.port 后重试",
                self._host,
                self._port,
                exc,
            )
            return

        self._runner = runner
        self._site = site
        suffix = "（请使用控制台密码登录）" if self._token else "（请先在本机设置密码）"
        logger.info("Web 控制台已启动：%s%s", self.base_url, suffix)

    async def stop(self) -> None:
        self._closing = True
        self.set_bootstrap_ready(False)
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._finish_stop(), name="webui-stop")
        await self._observe_owned(self._stop_task)

    async def _finish_stop(self) -> None:
        # No new saves are admitted after closing. Complete accepted file writes
        # and their credential/application effects before closing their resources.
        if self._configuration_tasks:
            await asyncio.gather(*tuple(self._configuration_tasks), return_exceptions=True)
        if self._maintenance is not None:
            await self._maintenance.close()
        await self._login.shutdown()
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._auth.invalidate()

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        path = request.path
        protected = path.startswith("/api/") or path.startswith(
            ("/voice/", "/health", "/oopz/", "/persona", "/memory")
        )
        browser_session = self._auth.authenticated(request)
        if (path == "/api/status" and request.method in {"GET", "HEAD"} and not self._token
                and self._auth.setup_allowed(request)):
            # Old update workers probe this URL without credentials. Keep only local
            # readiness/identity available while the installation awaits first setup.
            bridge = self._controller.snapshot()
            return web.json_response({"ok": True, "process": self._process_identity(),
                                      "bridge": {"runtime": {"supervisor_alive": bool(
                                          (bridge.get("runtime") or {}).get("supervisor_alive"))}}},
                                     headers={"Cache-Control": "no-store"})
        if (request.method not in {"GET", "HEAD", "OPTIONS"} and (browser_session or path in AUTH_PATHS)
                and not self._auth.same_origin(request)):
            return web.json_response({"ok": False, "error": "请从本控制台页面提交操作"}, status=403)
        if (protected and path not in AUTH_PATHS and not browser_session
                and not self._auth.plugin_authenticated(request)):
            return web.json_response({"ok": False, "error": "请登录控制台"}, status=401)
        return await handler(request)

    async def _observe_owned(self, task):
        """取消只中断等待者；先观察已受理工作完成，再传播取消。"""
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    raise
                cancelled = exc
            except Exception:
                break
        try:
            result = task.result()
        except Exception:
            if cancelled is not None:
                logger.exception("请求取消后，已受理的控制台操作失败")
                raise cancelled from None
            raise
        if cancelled is not None:
            raise cancelled
        return result

    async def _save_configuration(self, updates, *, restart_after=False):
        if self._closing:
            raise RuntimeError("控制台正在停止，暂不能保存配置")
        snapshot = copy.deepcopy(updates)
        task = asyncio.create_task(
            self._commit_configuration(snapshot, restart_after=restart_after),
            name="webui-configuration-save",
        )
        self._configuration_tasks.add(task)

        def finished(completed):
            self._configuration_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        return await self._observe_owned(task)

    async def _commit_configuration(self, updates, *, restart_after):
        # Serialize persistence and application together. Another save must not
        # commit a later token then have this task apply the earlier token last.
        async with self._configuration_lock:
            result = await asyncio.to_thread(config_editor.apply_updates, updates)
            if "token" in (result.get("changed") or {}).get("webui", []):
                self._change_password(str(updates["webui"]["token"]))
                result["reauth_required"] = True
                result.setdefault("notes", []).append("控制台密码与插件 API Token 已同步，请重新登录；插件请使用新 Token")
            # Runs on the main event loop, after successful persistence, even if
            # the HTTP waiter disconnected. It is part of this owned operation.
            await self._hot_reload_voice(result)
            if result.get("reauth_required") and restart_after is True:
                try:
                    await self._controller.restart("WebUI 保存配置并重连")
                    result["bridge_restart_requested"] = True
                except Exception:
                    logger.exception("配置已保存，但桥接重连失败")
                    result["bridge_restart_error"] = True
                    result.setdefault("notes", []).append("桥接重连失败，请重新登录后手动重连")
            return result

    async def _setup_password(self, password: str) -> None:
        await self._save_configuration({"webui": {"token": password}})

    def _change_password(self, password: str) -> None:
        self._token = password.strip()
        self._auth.invalidate()
        if self._voice_runtime is not None:
            # Credential rotation must not depend on successful model/session reload.
            self._voice_runtime.api_settings.token = self._token
        if self._maintenance is not None:
            self._maintenance.token = self._token

    # ------------------------------------------------------------------
    # 静态资源
    # ------------------------------------------------------------------

    async def _handle_index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(os.path.join(ASSETS_DIR, "index.html"), headers={"Cache-Control": "no-cache"})

    async def _handle_favicon(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(os.path.join(ASSETS_DIR, "favicon.svg"),
                                headers={"Content-Type": "image/svg+xml", "Cache-Control": "no-cache"})

    async def _handle_asset(self, request: web.Request) -> web.StreamResponse:
        name = os.path.basename(str(request.match_info.get("name") or ""))
        path = os.path.join(ASSETS_DIR, name)
        if not name or not os.path.isfile(path):
            raise web.HTTPNotFound()
        headers = {"Cache-Control": "no-cache"}
        if name.endswith(".svg"):
            headers["Content-Type"] = "image/svg+xml"
        return web.FileResponse(path, headers=headers)

    # ------------------------------------------------------------------
    # 状态 / 凭据
    # ------------------------------------------------------------------

    def _process_identity(self) -> dict[str, Any]:
        return {"pid": os.getpid(), "update_id": os.environ.get("OOPTRA_UPDATE_ID", ""),
                "executable": sys.executable, "bootstrap_ready": self.bootstrap_ready}

    async def _handle_status(self, _request: web.Request) -> web.StreamResponse:
        oopz_cfg = getattr(runtime_config, "OOPZ_CONFIG", {}) or {}
        onebot_cfg = getattr(runtime_config, "ONEBOT_V11_CONFIG", {}) or {}
        payload = {
            "ok": True,
            "bridge": self._controller.snapshot(),
            "process": {
                "version": __version__,
                **self._process_identity(),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "project_root": PROJECT_ROOT,
                "log_file": os.path.join(LOGS_DIR, "oopz_bot.log"),
            },
            "config": {
                "webui": {
                    "host": self._host,
                    "port": self._port,
                    "token_required": bool(self._token),
                },
                "oopz": {
                    "default_area": str(oopz_cfg.get("default_area") or ""),
                    "default_channel": str(oopz_cfg.get("default_channel") or ""),
                    "proxy": str(oopz_cfg.get("proxy") or ""),
                },
                "voice": self._voice_payload(),
                "onebot": {
                    "enabled": bool(onebot_cfg.get("enabled", False)),
                    "enable_ws_reverse": bool(onebot_cfg.get("enable_ws_reverse", False)),
                    "ws_reverse_url": str(onebot_cfg.get("ws_reverse_url") or ""),
                    "ws_reverse_targets": [
                        str(onebot_cfg.get(key) or "")
                        for key in (
                            "ws_reverse_url",
                            "ws_reverse_api_url",
                            "ws_reverse_event_url",
                        )
                        if str(onebot_cfg.get(key) or "").strip()
                    ],
                    "local_server": {
                        "enabled": bool(
                            onebot_cfg.get("enable_http") or onebot_cfg.get("enable_ws")
                        ),
                        "host": str(onebot_cfg.get("host") or ""),
                        "port": int(onebot_cfg.get("port") or 0),
                    },
                    "db_path": str(onebot_cfg.get("db_path") or ""),
                },
            },
        }
        from operations import evaluate_runtime_health

        payload["health"] = evaluate_runtime_health(payload)
        return web.json_response(payload)

    def _voice_payload(self) -> dict[str, Any]:
        runtime = self._voice_runtime
        if runtime is None:
            return {"enabled": False, "status": {}}
        try:
            status = runtime.agent.status()
        except Exception:
            status = {}
        return {
            "enabled": bool(runtime.agent_settings.enabled),
            "backend": runtime.agent_settings.backend,
            "status": status,
        }

    async def _handle_credentials(self, _request: web.Request) -> web.StreamResponse:
        return web.json_response({"ok": True, "credentials": credentials_summary()})

    # ------------------------------------------------------------------
    # 更新检查
    # ------------------------------------------------------------------

    async def _handle_update_check(self, _request: web.Request) -> web.StreamResponse:
        from operations import resolve_agent_proxy_url
        from webui.update_check import check_github_update

        # str() 必须包住整个表达式：写成 str(getattr(...)).get(...) 会对字符串调 .get()，
        # 无条件抛 AttributeError，导致「检查更新」永远 500。
        agent_config = getattr(runtime_config, "VOICE_AGENT_CONFIG", {}) or {}
        proxy = resolve_agent_proxy_url(
            str(agent_config.get("proxy", "") or "")
            if isinstance(agent_config, dict)
            else ""
        )
        payload = await check_github_update(proxy=proxy)
        return web.json_response(payload)

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def _log_lines(self) -> int:
        try:
            return int(self._config.get("log_lines") or DEFAULT_LOG_LINES)
        except (TypeError, ValueError):
            return DEFAULT_LOG_LINES

    async def _handle_logs_list(self, _request: web.Request) -> web.StreamResponse:
        return web.json_response(
            {"ok": True, "files": self._tailer.list_files(), "default": self._tailer.default_file}
        )

    async def _handle_logs_tail(self, request: web.Request) -> web.StreamResponse:
        name = request.query.get("file")
        try:
            lines = int(request.query.get("lines") or self._log_lines())
        except (TypeError, ValueError):
            lines = self._log_lines()
        try:
            payload = self._tailer.tail(name, lines)
        except FileNotFoundError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)
        return web.json_response({"ok": True, **payload})

    async def _handle_logs_stream(self, request: web.Request) -> web.StreamResponse:
        name = request.query.get("file")
        try:
            first_lines = int(request.query.get("lines") or self._log_lines())
        except (TypeError, ValueError):
            first_lines = self._log_lines()
        try:
            self._tailer.resolve(name)
        except FileNotFoundError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        try:
            async for chunk in self._tailer.stream(name, first_lines=first_lines):
                if not self._auth.authenticated(request) and not self._auth.plugin_authenticated(request):
                    break
                await response.write(chunk.encode("utf-8"))
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await response.write_eof()
        return response

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    async def _handle_config_get(self, _request: web.Request) -> web.StreamResponse:
        return web.json_response(
            {
                "ok": True,
                "path": config_editor.CONFIG_PATH,
                "groups": config_editor.schema_payload(),
            }
        )

    async def _handle_config_post(self, request: web.Request) -> web.StreamResponse:
        payload = await self._read_json(request)
        if payload is None:
            return web.json_response({"ok": False, "error": "请求体必须是 JSON"}, status=400)
        try:
            result = await self._save_configuration(
                payload.get("updates"), restart_after=payload.get("restart_after", False))
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("保存配置失败")
            return web.json_response({"ok": False, "error": f"保存失败：{exc}"}, status=500)

        message = "已写入 config.py"
        notes = result.get("notes") or []
        if notes:
            message = "已写入 config.py，应用结果：" + "；".join(str(n) for n in notes)
        elif result.get("restart_required"):
            message = "已写入 config.py，此项需重启 Ooptra 才能生效"
        return web.json_response({"ok": True, **result, "message": message})

    async def _hot_reload_voice(self, result: dict) -> None:
        """把刚保存的语音配置应用到运行中的 VoiceRuntime。"""
        changed = result.get("changed") or {}
        if not ({"voice", "voice_api", "auto_visit"} & set(changed)) and "token" not in changed.get("webui", []):
            return
        if self._voice_runtime is None and "token" in changed.get("webui", []):
            return
        try:
            from voice_agent.runtime import get_voice_runtime

            applied = await (self._voice_runtime or get_voice_runtime()).reload_settings()
        except Exception as exc:
            logger.exception("语音配置热重载失败")
            result["hot_reload_failed"] = True
            result.setdefault("notes", []).append(f"语音配置热重载失败：{exc}")
            return
        if applied.get("changed"):
            result["hot_reloaded_fields"] = applied["changed"]
        if applied.get("hot_reload_failed"):
            result["hot_reload_failed"] = True
        if applied.get("api_changed"):
            result["hot_reloaded_fields"] = [
                *(result.get("hot_reloaded_fields") or []),
                *applied["api_changed"],
            ]
        for note in applied.get("notes") or []:
            result.setdefault("notes", []).append(note)
        if applied.get("restart_keys"):
            result["restart_required"] = True

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------

    async def _handle_api_login(self, request: web.Request) -> web.StreamResponse:
        payload = await self._read_json(request)
        if payload is None:
            return web.json_response({"ok": False, "error": "请求体必须是 JSON"}, status=400)
        try:
            result = await self._login.login_with_password(
                payload.get("phone", ""),
                payload.get("password", ""),
                timeout=float(payload.get("timeout") or 60.0),
            )
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            logger.error("Oopz 账号密码登录失败: %s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, **result})

    async def _handle_browser_login_status(self, _request: web.Request) -> web.StreamResponse:
        return web.json_response({"ok": True, "task": self._login.browser_login_status()})

    async def _handle_browser_login_start(self, request: web.Request) -> web.StreamResponse:
        payload = await self._read_json(request) or {}
        try:
            task = self._login.start_browser_login(
                phone=str(payload.get("phone") or ""),
                password=str(payload.get("password") or ""),
                headless=bool(payload.get("headless", False)),
            )
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "task": task})

    async def _handle_browser_login_cancel(self, _request: web.Request) -> web.StreamResponse:
        return web.json_response({"ok": True, **await self._login.cancel_browser_login()})

    # ------------------------------------------------------------------
    # 桥接操作
    # ------------------------------------------------------------------

    async def _handle_bridge_restart(self, _request: web.Request) -> web.StreamResponse:
        await self._controller.restart("WebUI 手动重启")
        return web.json_response({"ok": True, "message": "已请求重启桥接"})

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    async def _read_json(request: web.Request) -> dict[str, Any] | None:
        try:
            payload = await request.json()
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None


__all__ = ["WebUIConsole"]
