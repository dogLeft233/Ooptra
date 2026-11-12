"""桥接运行状态：WebUI 读取的唯一事实来源。

所有写入都发生在事件循环内，因此不需要加锁；WebUI 只调用 snapshot() 取值。
"""

from __future__ import annotations

import copy
import time
from collections import Counter, deque
from typing import Any

RECENT_LIMIT = 20


def describe_error(exc: BaseException) -> str:
    """把异常压成一行可读文本，用于状态面板。"""
    text = str(exc).strip() or exc.__class__.__name__
    if isinstance(exc, OSError) and getattr(exc, "strerror", None):
        text = f"{text} ({exc.strerror})"
    return text


class LinkState:
    """单条长连接（Oopz 事件 WS / OneBot 反向 WS）的状态。"""

    __slots__ = (
        "attempts",
        "connected",
        "connected_since",
        "drops",
        "last_change",
        "last_error",
        "name",
        "target",
    )

    def __init__(self, name: str, target: str = "") -> None:
        self.name = name
        self.target = target
        self.connected = False
        self.connected_since = 0.0
        self.last_change = 0.0
        self.last_error = ""
        self.attempts = 0
        self.drops = 0

    def mark_attempt(self, target: str = "") -> None:
        if target:
            self.target = target
        self.attempts += 1
        self.last_change = time.time()

    def mark_connected(self, target: str = "") -> None:
        if target:
            self.target = target
        now = time.time()
        self.connected = True
        self.connected_since = now
        self.last_change = now
        self.last_error = ""

    def mark_disconnected(self, error: str = "") -> None:
        now = time.time()
        if self.connected:
            self.drops += 1
        self.connected = False
        self.connected_since = 0.0
        self.last_change = now
        if error:
            self.last_error = error

    def snapshot(self, now: float) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "connected": self.connected,
            "uptime_seconds": round(now - self.connected_since, 1) if self.connected else 0.0,
            "last_change": self.last_change or None,
            "last_error": self.last_error,
            "attempts": self.attempts,
            "drops": self.drops,
        }


class BridgeState:
    """桥接进程的可观测状态。"""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.oopz = LinkState("oopz_event_ws")
        self.reverse_ws = LinkState("onebot_reverse_ws")

        self.running = False
        self.last_error = ""
        self.restarts = 0

        self.self_uid = ""
        self.nickname = ""
        self.self_id = 0
        self.joined_areas = 0

        self.events_total = 0
        self.events_by_type: Counter[str] = Counter()
        self.last_event_at = 0.0
        self.last_event: dict[str, Any] = {}
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=RECENT_LIMIT)

        self.actions_total = 0
        self.last_action_at = 0.0
        self.recent_actions: deque[dict[str, Any]] = deque(maxlen=RECENT_LIMIT)

        self.login_task: dict[str, Any] = {"state": "idle", "message": "", "started_at": 0.0}

    # ------------------------------------------------------------------
    # 事件与 action 计数
    # ------------------------------------------------------------------

    def count_event(self, payload: dict[str, Any]) -> None:
        """作为 OneBot adapter 的事件 sink 调用，统计推给对端的事件。"""
        if not isinstance(payload, dict):
            return

        now = time.time()
        kind = self._event_kind(payload)
        self.events_total += 1
        self.events_by_type[kind] += 1
        self.last_event_at = now

        summary = {
            "time": now,
            "kind": kind,
            "post_type": str(payload.get("post_type") or ""),
            "group_id": payload.get("group_id"),
            "user_id": payload.get("user_id"),
            "preview": self._preview(payload),
        }
        self.last_event = summary
        self.recent_events.appendleft(summary)

    def count_action(self, action: str, params: dict[str, Any] | None = None) -> None:
        now = time.time()
        self.actions_total += 1
        self.last_action_at = now
        self.recent_actions.appendleft(
            {
                "time": now,
                "action": str(action or ""),
                "group_id": (params or {}).get("group_id"),
                "user_id": (params or {}).get("user_id"),
            }
        )

    @staticmethod
    def _event_kind(payload: dict[str, Any]) -> str:
        post_type = str(payload.get("post_type") or "unknown")
        if post_type == "message":
            return f"message.{payload.get('message_type') or 'unknown'}"
        if post_type == "notice":
            return f"notice.{payload.get('notice_type') or 'unknown'}"
        if post_type == "meta_event":
            return f"meta_event.{payload.get('meta_event_type') or 'unknown'}"
        return post_type

    @staticmethod
    def _preview(payload: dict[str, Any]) -> str:
        raw = str(payload.get("raw_message") or "")
        if not raw:
            message = payload.get("message")
            if isinstance(message, list):
                raw = "".join(
                    str((seg.get("data") or {}).get("text") or "")
                    for seg in message
                    if isinstance(seg, dict)
                )
            else:
                raw = str(message or "")
        raw = raw.replace("\n", " ").strip()
        return raw[:120]

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "runtime": {
                "running": self.running,
                "uptime_seconds": round(now - self.started_at, 1),
                "started_at": self.started_at,
                "restarts": self.restarts,
                "last_error": self.last_error,
            },
            "oopz": {
                **self.oopz.snapshot(now),
                "self_uid": self.self_uid,
                "nickname": self.nickname,
                "joined_areas": self.joined_areas,
            },
            "onebot": {
                **self.reverse_ws.snapshot(now),
                "self_id": self.self_id,
            },
            "traffic": {
                "events_total": self.events_total,
                "events_by_type": dict(self.events_by_type.most_common()),
                "last_event_at": self.last_event_at or None,
                "actions_total": self.actions_total,
                "last_action_at": self.last_action_at or None,
            },
            "recent_events": copy.deepcopy(list(self.recent_events)),
            "recent_actions": copy.deepcopy(list(self.recent_actions)),
            "login_task": dict(self.login_task),
        }


__all__ = ["BridgeState", "LinkState", "describe_error"]
