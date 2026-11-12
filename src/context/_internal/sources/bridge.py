"""Bounded, authenticated-by-the-HTTP-layer RPC to an AstrBot plugin worker."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from context.api import TurnRecorder

logger = logging.getLogger(__name__)
class ContextBridge:
    def __init__(self, *, timeout: float = 4.0, outbox_path: Path | None = None) -> None:
        self.timeout = timeout
        self.path = outbox_path
        self.session_id = uuid.uuid4().hex
        self._room: dict[str, str] = {}
        self._context: dict[str, Any] = {}
        self._history: list[dict[str, str]] = []
        self._pending: dict[str, tuple[dict, asyncio.Future]] = {}
        self._outbox: dict[str, dict] = {}
        self._leased: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._last_poll = 0.0
        self.peer_id = ""
        self._state = "offline"
        self._error = ""
        self._last_write = ""
        if self.path and self.path.exists():
            try:
                if self.path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("Outbox exceeds size limit")
                rows = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(rows, list) or len(rows) > 128:
                    raise ValueError("Invalid voice outbox")
                for row in rows:
                    if not isinstance(row, dict) or row.get("operation") not in {"commit", "remember"}:
                        raise ValueError("Invalid voice outbox event")
                    if (not isinstance(row.get("request_id"), str) or not row["request_id"]
                            or row["request_id"] in self._outbox
                            or not isinstance(row.get("session_id"), str)
                            or not all(isinstance(row.get(key), dict) for key in ("room", "scope", "payload"))):
                        raise ValueError("Invalid voice outbox fields")
                    self._outbox[row["request_id"]] = copy.deepcopy(row)
            except (OSError, ValueError, KeyError) as exc:
                raise ValueError("语音记忆待写队列读取失败，请检查本地文件；未启动写入") from exc

    @property
    def context(self) -> dict[str, Any]:
        """已准备来源的独立快照；修改返回值不会改变来源。"""
        return copy.deepcopy(self._context)

    @property
    def history(self) -> list[dict[str, str]]:
        """当前来源近期历史的独立快照。"""
        return copy.deepcopy(self._history)

    @property
    def connected(self) -> bool:
        return bool(self._last_poll and time.monotonic() - self._last_poll < 35)

    @property
    def active(self) -> bool:
        # A running Live session keeps its prepared persona when its bridge
        # disconnects. Only a room/context rebuild can change that source.
        return self._context.get("ok") is True

    @property
    def persona(self) -> str:
        return str(self._context.get("persona") or "") if self.active else ""

    def status(self) -> dict[str, Any]:
        return {"protocol": 1, "source_selection": True, "connected": self.connected, "active": self.active,
                "source_kind": self._context.get("source_kind", ""),
                "source_label": self._context.get("source_label", ""),
                "platform_id": self._context.get("platform_id", ""),
                "persona_umo": self._context.get("umo", ""),
                "memory_umos": [row.get("umo", "") for row in self._context.get("memory_scopes", [])],
                "state": self._state if self.connected else "offline", "error": self._error,
                "group_id": self._context.get("group_id", ""),
                "persona_id": self._context.get("persona_id", ""),
                "provider": self._context.get("provider", ""),
                "memory_error": self._context.get("memory_error", ""),
                "pending_requests": len(self._pending), "pending_writes": len(self._outbox), "last_write": self._last_write,
                "session_id": self.session_id}

    def clear(self) -> None:
        self.session_id = uuid.uuid4().hex
        self._context = {}
        self._history.clear()
        for request_id, (event, future) in list(self._pending.items()):
            if event["operation"] == "bindings":
                continue
            if not future.done():
                future.set_result({"ok": False, "error": "语音会话已切换"})
            self._pending.pop(request_id, None)

    async def prepare(self, area: str, channel: str) -> None:
        self.clear()
        self._room = {"area": area, "channel": channel}
        if not self.connected:
            self._state = "offline"
            return
        session = self.session_id
        result = await self.request("prepare", {})
        if self.session_id != session:
            return
        if result.get("ok") is True:
            self._context = copy.deepcopy(result)
            self._state, self._error = "ready", ""
        else:
            self._state, self._error = "unavailable", str(result.get("error") or "上下文不可用")

    def _event(self, operation: str, payload: dict) -> dict:
        scope = {key: self._context.get(key, "") for key in
                 ("group_id", "umo", "persona_id", "memory_persona_id", "provider", "platform_id", "bot_id", "source_kind", "memory_scopes", "memory_route_id")}
        return {"protocol": 1, "request_id": uuid.uuid4().hex, "session_id": self.session_id,
                "operation": operation, "room": dict(self._room), "scope": copy.deepcopy(scope),
                "created_at": time.time(), "payload": copy.deepcopy(payload)}

    async def request(self, operation: str, payload: dict) -> dict:
        if not self.connected or len(self._pending) >= 16:
            return {"ok": False, "error": "AstrBot 桥接离线或忙碌"}
        if operation == "remember" and self._write_count() >= 128:
            return {"ok": False, "error": "语音记忆待写队列已满，请等待已有目标写回后再试"}
        event = self._event(operation, payload)
        if operation == "remember":
            self._retry_write(event, {})
        future = asyncio.get_running_loop().create_future()
        self._pending[event["request_id"]] = (event, future)
        self._wake.set()
        try:
            return await asyncio.wait_for(future, max(self.timeout, 20) if operation in {"bindings", "remember"} else self.timeout)
        except TimeoutError:
            self._error = "AstrBot 请求超时"
            result = {"ok": False, "error": self._error}
            return self._retry_write(event, result) if operation == "remember" else result
        finally:
            self._pending.pop(event["request_id"], None)
            if event["request_id"] not in self._outbox:
                self._leased.pop(event["request_id"], None)

    def _retry_write(self, event: dict, result: dict) -> dict:
        if event["request_id"] not in self._outbox and len(self._outbox) >= 128:
            self._error = "语音记忆待写队列已满，失败目标未排队"
            return {**result, "queued": False, "error": self._error}
        prior = self._outbox.get(event["request_id"])
        self._outbox[event["request_id"]] = event
        try:
            self._persist()
        except OSError:
            if prior is None:
                self._outbox.pop(event["request_id"], None)
            else:
                self._outbox[event["request_id"]] = prior
            raise
        self._wake.set()
        return {**result, "queued": True, "request_id": event["request_id"]}

    def _write_count(self) -> int:
        # In-flight explicit writes reserve a slot for their original retry ID.
        return len(set(self._outbox) | {key for key, (event, _) in self._pending.items()
                                     if event["operation"] == "remember"})

    async def tool(self, name: str, args: Any, *, speaker: str = "") -> dict:
        if not self.active or name not in {"astrbot_recall", "astrbot_remember"}:
            return {"ok": False, "error": "共享记忆不可用"}
        field = "query" if name == "astrbot_recall" else "content"
        if not isinstance(args, dict) or not isinstance(args.get(field), str):
            return {"ok": False, "error": "记忆工具参数不正确"}
        text = args[field].strip()
        if not text or len(text) > 2000:
            return {"ok": False, "error": "记忆工具内容须为 1 至 2000 个字"}
        return await self.request("recall" if field == "query" else "remember",
                                  {field: text, "speaker": speaker})

    def bind_turn(self, *, speaker: str) -> TurnRecorder:
        """固定原来源与授权，至多持久提交一次；迟到结果不更新新会话历史。"""
        allowed = self._context.get("ok") is True and (
            "memory_scopes" not in self._context
            or bool(self._context["memory_scopes"] and self._context.get("provider")))
        event = copy.deepcopy(self._event("commit", {}))
        recorded = False

        def record(user_text: str, reply_text: str, *, interrupted: bool = False) -> None:
            nonlocal recorded
            if recorded or not allowed or not user_text.strip():
                return
            if self._write_count() >= 128:
                self._error = "语音记忆待写队列已满，本轮未保存"
                logger.error(self._error)
                return
            event["payload"] = {"user_text": user_text[:4000],
                                "reply_text": "" if interrupted else reply_text[:4000],
                                "speaker": speaker, "interrupted": interrupted}
            self._outbox[event["request_id"]] = event
            try:
                self._persist()
            except OSError:
                self._outbox.pop(event["request_id"], None)
                raise
            recorded = True
            if event["session_id"] == self.session_id:
                self._history.append({"role": "user", "content": user_text[:2000]})
                if event["payload"]["reply_text"]:
                    self._history.append({"role": "assistant", "content": reply_text[:2000]})
                self._history = self._history[-12:]
            self._wake.set()
        return record

    def record_turn(self, user_text: str, reply_text: str, *, speaker: str,
                    interrupted: bool = False) -> None:
        self.bind_turn(speaker=speaker)(user_text, reply_text, interrupted=interrupted)

    def _persist(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(list(self._outbox.values()), ensure_ascii=False),
                                 encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            self._error = "语音记忆队列落盘失败"
            raise

    async def poll(self, *, wait: float = 20.0, client_id: str = "") -> list[dict]:
        if client_id and self.peer_id and self.peer_id != client_id and self.connected:
            raise ValueError("已有其他 AstrBot 实例接入当前 Ooptra，请先停用旧连接")
        if client_id:
            self.peer_id = client_id
        self._last_poll = time.monotonic()
        deadline = time.monotonic() + min(20, max(0, wait))
        while True:
            now = time.monotonic()
            events = list({event["request_id"]: event for event in
                           [item[0] for item in self._pending.values()] + list(self._outbox.values())}.values())
            ready = [event for event in events
                     if now - self._leased.get(event["request_id"], -100)
                     >= (25 if event["operation"] in {"bindings", "remember", "commit"} else 8)][:8]
            if ready:
                for event in ready:
                    self._leased[event["request_id"]] = now
                return copy.deepcopy(ready)
            if now >= deadline:
                return []
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), min(1, deadline - now))

    def resolve(self, request_id: str, result: dict) -> bool:
        pending = self._pending.get(request_id)
        if request_id in self._outbox and pending is not None and not pending[1].done():
            # Explicit writes are persisted before the worker can receive them.
            # Complete the local waiter independently of later durable retries.
            if result.get("retry") is True:
                pending[1].set_result({**copy.deepcopy(result), "queued": True, "request_id": request_id})
                self._error = str(result.get("error") or "记忆写入待重试")[:200]
                return True
            event = self._outbox.pop(request_id)
            try:
                self._persist()
            except OSError:
                self._outbox[request_id] = event
                raise
            self._leased.pop(request_id, None)
            self._last_write = str(result.get("state") or "captured") if result.get("ok") is True else "failed"
            pending[1].set_result(copy.deepcopy(result))
            return True
        if request_id in self._outbox:
            self._last_write = str(result.get("state") or "captured") if result.get("ok") is True else "failed"
            if result.get("retry") is True:
                self._error = str(result.get("error") or "记忆写入待重试")[:200]
                return True
            event = self._outbox.pop(request_id)
            try:
                self._persist()
            except OSError:
                self._outbox[request_id] = event
                raise
            self._leased.pop(request_id, None)
            if result.get("ok") is not True:
                self._error = str(result.get("error") or "记忆写入失败")[:200]
            else:
                self._error = ""
            return True
        item = self._pending.get(request_id)
        if item is None or item[1].done():
            return False
        if item[0]["operation"] != "bindings" and item[0]["session_id"] != self.session_id:
            return False
        if item[0]["operation"] == "remember" and result.get("ok") is not True and result.get("retry") is True:
            result = self._retry_write(item[0], result)
        item[1].set_result(copy.deepcopy(result))
        return True
