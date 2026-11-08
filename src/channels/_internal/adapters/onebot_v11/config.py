from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT_PATH as _PROJECT_ROOT


@dataclass(slots=True)
class OneBotV11ServerConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 6700
    access_token: str = ""
    secret: str = ""
    db_path: str = str(_PROJECT_ROOT / "data" / "onebot_v11.sqlite3")

    enable_http: bool = True
    enable_ws: bool = True
    enable_http_post: bool = False
    enable_ws_reverse: bool = False

    http_post_urls: list[str] = field(default_factory=list)
    http_post_timeout: float = 0.0

    ws_reverse_url: str = ""
    ws_reverse_api_url: str = ""
    ws_reverse_event_url: str = ""
    ws_reverse_reconnect_interval: float = 3.0

    send_connect_event: bool = True

    heartbeat_enabled: bool = True
    heartbeat_interval: float = 15.0

    member_list_max: int = 5000

    enable_area_scoped_group_ban: bool = False
    enable_set_group_kick_as_area_kick: bool = False
    enable_set_group_leave_as_area_leave: bool = False
    enable_set_group_admin_as_area_role: bool = False
    group_admin_role_id: int = 0


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _resolve_db_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return str(_PROJECT_ROOT / "data" / "onebot_v11.sqlite3")
    path = Path(raw)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return str(path)


def get_onebot_v11_config(raw: dict[str, Any]) -> OneBotV11ServerConfig:
    enabled = _as_bool(raw.get("enabled"), False)

    cfg = OneBotV11ServerConfig(
        enabled=enabled,
        host=str(raw.get("host") or "127.0.0.1"),
        port=int(raw.get("port") or 6700),
        access_token=str(raw.get("access_token") or ""),
        secret=str(raw.get("secret") or ""),
        db_path=_resolve_db_path(raw.get("db_path")),
        enable_http=_as_bool(raw.get("enable_http"), True),
        enable_ws=_as_bool(raw.get("enable_ws"), True),
        enable_http_post=_as_bool(raw.get("enable_http_post"), False),
        enable_ws_reverse=_as_bool(raw.get("enable_ws_reverse"), False),
        http_post_urls=_as_list(raw.get("http_post_urls")),
        http_post_timeout=float(raw.get("http_post_timeout") or 0.0),
        ws_reverse_url=str(raw.get("ws_reverse_url") or ""),
        ws_reverse_api_url=str(raw.get("ws_reverse_api_url") or ""),
        ws_reverse_event_url=str(raw.get("ws_reverse_event_url") or ""),
        ws_reverse_reconnect_interval=float(raw.get("ws_reverse_reconnect_interval") or 3.0),
        send_connect_event=_as_bool(raw.get("send_connect_event"), True),
        heartbeat_enabled=_as_bool(raw.get("heartbeat_enabled"), True),
        heartbeat_interval=float(raw.get("heartbeat_interval") or 15.0),
        member_list_max=int(raw.get("member_list_max") or 5000),
        enable_area_scoped_group_ban=_as_bool(raw.get("enable_area_scoped_group_ban"), False),
        enable_set_group_kick_as_area_kick=_as_bool(raw.get("enable_set_group_kick_as_area_kick"), False),
        enable_set_group_leave_as_area_leave=_as_bool(raw.get("enable_set_group_leave_as_area_leave"), False),
        enable_set_group_admin_as_area_role=_as_bool(raw.get("enable_set_group_admin_as_area_role"), False),
        group_admin_role_id=int(raw.get("group_admin_role_id") or 0),
    )
    return cfg
