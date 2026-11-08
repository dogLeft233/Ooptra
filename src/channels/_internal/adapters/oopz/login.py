"""WebUI 触发的 Oopz 登录：账号密码（API）与网页版（浏览器）两条路径。"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from core.logger_config import get_logger
from core.proxy_utils import resolve_proxy_settings_with_env
from oopz_sdk.auth import login_with_password as sdk_login_with_password
from oopz_sdk.auth import login_with_playwright_password
from oopz_sdk.utils.jwt import decode_jwt_payload

logger = get_logger("WebUILogin")

BROWSER_LOGIN_TIMEOUT = 300.0


def mask(value: Any, keep: int = 4) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}***{text[-keep:]}"


def credentials_summary(configuration) -> dict[str, Any]:
    oopz_cfg, _, _ = configuration.snapshot()
    token = str(oopz_cfg.get("jwt_token") or "")
    expires_at = decode_jwt_payload(token).get("exp") if token else None
    expires_in = (
        int(expires_at - time.time()) if isinstance(expires_at, (int, float)) else None
    )
    return {
        "login_phone": mask(oopz_cfg.get("login_phone")),
        "person_uid": mask(oopz_cfg.get("person_uid")),
        "device_id": mask(oopz_cfg.get("device_id")),
        "jwt_token": mask(token, keep=10),
        "app_version": str(oopz_cfg.get("app_version") or ""),
        "has_password": bool(oopz_cfg.get("login_password")),
        "has_private_key": bool(configuration.load_private_key()),
        "expires_at": expires_at,
        "expires_in_seconds": expires_in,
    }


class OopzLoginService:
    """登录成功后写回 config.py/private_key.py，并重建桥接连接。"""

    def __init__(self, controller: Any, configuration: Any) -> None:
        self._controller = controller
        self._configuration = configuration
        self._task_state = {"state": "idle", "message": "", "started_at": 0.0}
        self._browser_task: asyncio.Task[None] | None = None

    def credentials_summary(self):
        return credentials_summary(self._configuration)

    # ------------------------------------------------------------------
    # 账号密码（API）
    # ------------------------------------------------------------------

    async def login_with_password(
        self,
        phone: str,
        password: str,
        *,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        phone = str(phone or "").strip()
        password = str(password or "")
        if not phone or not password:
            raise ValueError("手机号与密码不能为空")
        seconds = max(10.0, min(float(timeout or 60.0), 300.0))

        logger.info("开始 Oopz 账号密码登录: %s", mask(phone))
        credentials = await sdk_login_with_password(phone, password, timeout=seconds)
        saved = await self._configuration.persist_credentials({"device_id": credentials.device_id, "person_uid": credentials.person_uid, "jwt_token": credentials.jwt_token, "private_key_pem": credentials.private_key_pem, "app_version": credentials.app_version})
        await self._controller.restart("Oopz 凭据已更新")
        logger.info("Oopz 账号密码登录成功，已写入 %s", "、".join(saved))
        return {
            "saved": saved,
            "credentials": self.credentials_summary(),
            "message": "登录成功，凭据已保存并重连",
        }

    # ------------------------------------------------------------------
    # 网页版（Playwright 浏览器）
    # ------------------------------------------------------------------

    def browser_login_status(self) -> dict[str, Any]:
        return dict(self._task_state)

    def start_browser_login(
        self,
        *,
        phone: str = "",
        password: str = "",
        headless: bool = False,
    ) -> dict[str, Any]:
        if self._browser_task is not None and not self._browser_task.done():
            raise ValueError("已有一个浏览器登录任务在执行")

        oopz_cfg = self._configuration.snapshot()[0]
        phone = str(phone or "").strip() or str(oopz_cfg.get("login_phone") or "").strip()
        password = str(password or "") or str(oopz_cfg.get("login_password") or "")
        if not phone or not password:
            raise ValueError("浏览器登录需要手机号与密码（自动填充后再人工过验证码）")

        self._task_state = {"state": "idle", "message": "", "started_at": 0.0}
        self._set_task_state(
            "running",
            "已启动浏览器，请在弹出的窗口中完成验证（如滑块/短信）",
        )
        self._browser_task = asyncio.create_task(
            self._run_browser_login(phone, password, bool(headless)),
            name="oopz-browser-login",
        )
        return self.browser_login_status()

    async def cancel_browser_login(self) -> dict[str, Any]:
        task = self._browser_task
        if task is None or task.done():
            return {"cancelled": False, "message": "没有正在执行的浏览器登录"}
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return {"cancelled": True, "message": "已取消浏览器登录"}

    async def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            await self.cancel_browser_login()

    async def _run_browser_login(self, phone: str, password: str, headless: bool) -> None:
        try:
            credentials = await login_with_playwright_password(
                phone,
                password,
                timeout=BROWSER_LOGIN_TIMEOUT,
                headless=headless,
                proxy=self._browser_proxy(),
            )
        except asyncio.CancelledError:
            self._set_task_state("cancelled", "浏览器登录已取消")
            raise
        except Exception as exc:
            logger.error("Oopz 网页版登录失败: %s", exc)
            self._set_task_state("failed", str(exc) or exc.__class__.__name__)
            return

        try:
            saved = await self._configuration.persist_credentials({"device_id": credentials.device_id, "person_uid": credentials.person_uid, "jwt_token": credentials.jwt_token, "private_key_pem": credentials.private_key_pem, "app_version": credentials.app_version})
            await self._controller.restart("Oopz 网页版登录成功")
        except Exception as exc:
            logger.error("网页版登录凭据保存失败: %s", exc)
            self._set_task_state("failed", f"凭据保存失败：{exc}")
            return

        logger.info("Oopz 网页版登录成功，已写入 %s", "、".join(saved))
        self._set_task_state("ok", "登录成功，凭据已保存并重连", saved=saved)

    def _browser_proxy(self) -> str | None:
        try:
            settings = resolve_proxy_settings_with_env(
                (self._configuration.snapshot()[0]).get("proxy")
            )
        except Exception as exc:
            logger.debug("解析浏览器登录代理失败: %s", exc)
            return None
        return str(settings.server) if settings.enabled and settings.server else None

    def _set_task_state(self, state: str, message: str, **extra: Any) -> None:
        payload: dict[str, Any] = {
            "state": state,
            "message": message,
            "started_at": float(self._task_state.get("started_at") or time.time()),
        }
        if state != "running":
            payload["finished_at"] = time.time()
        payload.update(extra)
        self._task_state = payload


__all__ = ["OopzLoginService", "credentials_summary", "mask"]
