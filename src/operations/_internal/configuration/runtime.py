"""运行时环境变量覆盖与配置校验。

环境变量只覆盖少量高频项，方便容器/服务方式部署；其余一律以 config.py 为准。
"""

from __future__ import annotations

import os
from typing import Any

import config as runtime_config

_REVERSE_URL_FIELDS = ("ws_reverse_url", "ws_reverse_api_url", "ws_reverse_event_url")
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_VALUES


def apply_runtime_overrides() -> None:
    """用环境变量覆盖部分运行时配置。"""
    oopz_cfg = getattr(runtime_config, "OOPZ_CONFIG", None)
    if isinstance(oopz_cfg, dict):
        proxy = os.environ.get("BOT_OOPZ_PROXY")
        if proxy is not None:
            oopz_cfg["proxy"] = proxy

    onebot_cfg = getattr(runtime_config, "ONEBOT_V11_CONFIG", None)
    if isinstance(onebot_cfg, dict):
        reverse_url = os.environ.get("BOT_ONEBOT_REVERSE_URL")
        if reverse_url:
            onebot_cfg["ws_reverse_url"] = reverse_url.strip()

    webui_cfg = getattr(runtime_config, "WEBUI_CONFIG", None)
    if isinstance(webui_cfg, dict):
        host = os.environ.get("BOT_WEBUI_HOST")
        port = os.environ.get("BOT_WEBUI_PORT")
        token = os.environ.get("BOT_WEBUI_TOKEN")
        if host:
            webui_cfg["host"] = host.strip()
        if port:
            webui_cfg["port"] = _as_int(port, "BOT_WEBUI_PORT")
        # Persisted shared password is canonical; environment can bootstrap an empty installation.
        if token and not str(webui_cfg.get("token") or "").strip():
            webui_cfg["token"] = token.strip()


def _as_int(value: Any, field: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数: {value!r}") from exc


def _check_port(value: Any, field: str) -> None:
    port = _as_int(value, field)
    if not 1 <= port <= 65535:
        raise ValueError(f"{field} 不在合法端口范围 1-65535: {port}")


def validate_runtime_config() -> None:
    """在建立任何连接之前校验配置，避免带着明显错误空转。"""
    onebot_cfg = getattr(runtime_config, "ONEBOT_V11_CONFIG", None)
    if isinstance(onebot_cfg, dict) and _as_bool(onebot_cfg.get("enabled")):
        _check_port(onebot_cfg.get("port") or 6700, "ONEBOT_V11_CONFIG.port")
        urls = [
            str(onebot_cfg.get(field) or "").strip()
            for field in _REVERSE_URL_FIELDS
        ]
        for url in urls:
            if url and not url.lower().startswith(("ws://", "wss://")):
                raise ValueError(f"反向 WebSocket 地址必须以 ws:// 或 wss:// 开头: {url}")
        if _as_bool(onebot_cfg.get("enable_ws_reverse")) and not any(urls):
            raise ValueError(
                "ONEBOT_V11_CONFIG 已启用反向 WebSocket，"
                "但 ws_reverse_url / ws_reverse_api_url / ws_reverse_event_url 全为空"
            )

    webui_cfg = getattr(runtime_config, "WEBUI_CONFIG", {}) or {}
    if isinstance(webui_cfg, dict):
        _check_port(webui_cfg.get("port") or 3090, "WEBUI_CONFIG.port")
        if not str(webui_cfg.get("host") or "").strip():
            raise ValueError("WEBUI_CONFIG.host 不能为空")
        token = webui_cfg.get("token")
        if token is not None and not isinstance(token, str):
            raise ValueError("WEBUI_CONFIG.token 必须是字符串")


__all__ = ["apply_runtime_overrides", "validate_runtime_config"]
