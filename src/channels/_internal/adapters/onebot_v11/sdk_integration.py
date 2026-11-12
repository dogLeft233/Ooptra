"""项目对 Oopz-SDK OneBot v11 adapter 的缺失能力补丁。"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Mapping
from typing import Any

from core.logger_config import get_logger
from oopz_sdk.adapters.onebot.v11.message import to_v11_message
from oopz_sdk.adapters.onebot.v11.types import (
    make_message_source,
    make_user_source,
    parse_bool,
    parse_oopz_timestamp,
    require_int,
)
from oopz_sdk.models.message import Message

logger = get_logger("OneBotV11SdkIntegration")


def find_sdk_onebot_v11(bot) -> Any | None:
    for adapter in getattr(bot, "adapters", ()):
        if getattr(adapter, "protocol", "") == "onebot.v11":
            return adapter
    return None


async def dispatch_payload(adapter: Any, payload: dict[str, Any]) -> None:
    """把项目补充事件送入 SDK adapter 的既有 event sinks。"""
    adapter._event_queue.append(payload)
    for sink in list(adapter._event_sinks):
        try:
            result = sink(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("推送 OneBot v11 成员通知失败")


class OneBotV11Supplement:
    """补齐 SDK v0.15.0 尚未覆盖的成员通知、heartbeat 与管理员映射。"""

    def __init__(self, adapter: Any, gateway: Any, config: Any) -> None:
        self.adapter = adapter
        self.gateway = gateway
        self.config = config
        self._stop_event = asyncio.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._original_actions: dict[str, Any] = {}
        self._install_action("get_group_msg_history", self.get_group_msg_history)
        if int(config.member_list_max or 0) > 0:
            self._sdk_get_group_member_list = adapter._actions.get("get_group_member_list")
            self._install_action("get_group_member_list", self.get_group_member_list)
        else:
            self._sdk_get_group_member_list = None
        if config.enable_set_group_admin_as_area_role:
            self._install_action("set_group_admin", self.set_group_admin)

    def _install_action(self, name: str, handler: Any) -> None:
        if name in self.adapter._actions:
            self._original_actions[name] = self.adapter._actions[name]
        self.adapter._actions[name] = handler

    def start(self, supervisor=None) -> None:
        if not self.config.heartbeat_enabled or self.config.heartbeat_interval <= 0:
            return
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        coroutine = self._heartbeat_loop()
        self._heartbeat_task = (
            supervisor.create(coroutine, name="onebot-v11-heartbeat")
            if supervisor is not None
            else asyncio.create_task(coroutine, name="onebot-v11-heartbeat")
        )

    async def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, timeout))
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for name in ("get_group_msg_history", "get_group_member_list", "set_group_admin"):
            current = self.adapter._actions.get(name)
            if getattr(current, "__self__", None) is not self:
                continue
            original = self._original_actions.get(name)
            if original is None:
                self.adapter._actions.pop(name, None)
            else:
                self.adapter._actions[name] = original

    async def set_group_admin(self, params: Mapping[str, Any]) -> dict[str, Any]:
        role_id = int(self.config.group_admin_role_id or 0)
        if role_id <= 0:
            raise ValueError("group_admin_role_id 未配置")
        group_id = require_int(params, "group_id")
        user_id = require_int(params, "user_id")
        enable = parse_bool(params.get("enable"), default=True)
        area, _channel = self.adapter._resolve_group_id(group_id)
        uid = self.adapter._resolve_user_id(user_id)
        result = await self.gateway.edit_user_role(
            uid,
            role_id,
            add=enable,
            area=area,
        )
        if "error" in result:
            raise RuntimeError(str(result["error"]))
        return {}

    async def get_group_member_list(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        if self._sdk_get_group_member_list is None:
            return []
        members = await self._sdk_get_group_member_list(params)
        limit = max(1, int(self.config.member_list_max or 1))
        return list(members or [])[:limit]

    async def get_group_msg_history(self, params: Mapping[str, Any]) -> dict[str, Any]:
        group_id = require_int(params, "group_id")
        area, channel = self.adapter._resolve_group_id(group_id)
        try:
            count = int(params.get("count") or 20)
        except (TypeError, ValueError):
            raise ValueError("count must be an integer") from None
        count = min(100, max(1, count))
        raw_messages = await self.gateway.get_channel_messages(
            area=area,
            channel=channel,
            size=count,
        )
        messages: list[dict[str, Any]] = []
        for raw in raw_messages:
            if not isinstance(raw, Mapping):
                continue
            messages.append(
                await self._history_message(dict(raw), area, channel, group_id)
            )
        messages.reverse()
        return {"messages": messages}

    async def _history_message(
        self,
        raw: dict[str, Any],
        area: str,
        channel: str,
        group_id: int,
    ) -> dict[str, Any]:
        raw = {**raw, "area": area, "channel": channel}
        message = Message.from_api(raw)
        uid = message.sender_id

        def _ids() -> tuple[int, int]:
            user_id = (
                self.adapter.ids.createId(make_user_source(uid)).number
                if uid
                else 0
            )
            message_id = self.adapter.ids.createId(
                make_message_source(
                    area=area,
                    channel=channel,
                    message_id=message.message_id,
                )
            ).number
            return user_id, message_id

        user_id, message_id = await asyncio.to_thread(_ids)
        segments = await asyncio.to_thread(
            lambda: to_v11_message(message, ids=self.adapter.ids)
        )
        nickname = ""
        try:
            from channels._internal.adapters.oopz.name_resolver import get_resolver

            nickname = get_resolver().user_cached(uid)
        except Exception:
            pass
        payload = {
            "time": parse_oopz_timestamp(message.timestamp),
            "self_id": self.adapter.self_id,
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": message_id,
            "user_id": user_id,
            "group_id": group_id,
            "sender": {"user_id": user_id, "nickname": nickname},
            "message": segments,
            "raw_message": _cq_from_segments(segments),
            "extra": {
                "oopz_area_id": area,
                "oopz_channel_id": channel,
                "oopz_user_id": uid,
                "oopz_message_id": message.message_id,
            },
        }
        await asyncio.to_thread(self.adapter._save_message_event_mapping, payload)
        payload.pop("self_id", None)
        payload.pop("post_type", None)
        payload.pop("sub_type", None)
        return payload

    async def _heartbeat_loop(self) -> None:
        interval = max(0.05, float(self.config.heartbeat_interval))
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            await dispatch_payload(
                self.adapter,
                {
                    "time": int(time.time()),
                    "self_id": self.adapter.self_id,
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "status": {"online": True, "good": True},
                    "interval": int(interval * 1000),
                },
            )


def _cq_from_segments(segments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for segment in segments:
        data = segment.get("data") if isinstance(segment, Mapping) else {}
        data = data if isinstance(data, Mapping) else {}
        kind = str(segment.get("type") or "text") if isinstance(segment, Mapping) else "text"
        if kind == "text":
            text = str(data.get("text") or "")
            parts.append(text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;"))
        elif kind == "at":
            parts.append(f"[CQ:at,qq={data.get('qq', '')}]")
        elif kind == "image":
            value = str(data.get("file") or data.get("url") or "")
            parts.append(f"[CQ:image,file={value}]")
    return "".join(parts)


__all__ = [
    "OneBotV11Supplement",
    "dispatch_payload",
    "find_sdk_onebot_v11",
]
