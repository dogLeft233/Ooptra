"""Bounded, authenticated-by-the-HTTP-layer RPC to an AstrBot plugin worker."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from context._internal.memory.ledger import ContextLedger

from context.models import BindingRequest, PollRequest, PollResult, RpcReceipt, RpcResolution

logger = logging.getLogger(__name__)
class ContextBridge:
    def __init__(self, *, timeout: float = 4.0, ledger: ContextLedger, owns_ledger: bool = False) -> None:
        self._ledger = ledger
        self._owns_ledger = owns_ledger
        self._closed = False
        self._resolved: set[str] = set()
        self.timeout = timeout
        self.session_id = uuid.uuid4().hex
        self._room: dict[str, str] = {}
        self._context: dict[str, Any] = {}
        self._pending: dict[str, tuple[dict, asyncio.Future]] = {}
        self._outbox: dict[str, dict] = {}
        self._leased: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._last_poll = 0.0
        self.peer_id = ""
        self._state = "offline"
        self._error = ""
        self._last_write = ""
        self._outbox = ledger.ready()

    @property
    def context(self) -> dict[str, Any]:
        """已准备来源的独立快照；修改返回值不会改变来源。"""
        return copy.deepcopy(self._context)

    @property
    def history(self) -> list[dict[str, str]]:
        """当前来源近期历史的独立快照。"""
        if not self.active:
            return []
        return [{"role": row.role, "content": row.content} for row in self._ledger.scoped_history(
            room=json.dumps(self._room, ensure_ascii=False, sort_keys=True, separators=(",", ":")), source=self.session_id, limit=12)]

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
        if self._closed or not self.connected or len(self._pending) >= 16:
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

    def record_turn(self, user_text: str, reply_text: str, *, speaker: str,
                    interrupted: bool = False) -> None:
        # Keep already prepared scope when the worker is temporarily disconnected.
        if self._context.get("ok") is not True or not user_text.strip():
            return
        if "memory_scopes" in self._context and (not self._context["memory_scopes"] or not self._context.get("provider")):
            return
        if self._write_count() >= 128:
            self._error = "语音记忆待写队列已满，本轮未保存"
            logger.error(self._error)
            return
        event = self._event("commit", {"user_text": user_text[:4000],
                            "reply_text": "" if interrupted else reply_text[:4000],
                            "speaker": speaker, "interrupted": interrupted})
        from context.models import ConfirmedTurn
        receipt = self._ledger.commit("bridge:" + event["request_id"], event,
            ConfirmedTurn(user_text, "" if interrupted else reply_text,
                          "interrupted" if interrupted else "completed" if reply_text.strip() else "silent", speaker,
                          self._room.get("channel", "")),
            room=json.dumps(self._room, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            session=self.session_id, source=self.session_id, event=event)
        if receipt.delivery_admission == "accepted":
            self.publish_committed()

    def _persist(self, terminal: tuple[str, dict] | None = None) -> None:
        self._ledger.replace_pending(self._outbox, terminal)

    async def poll(self, *, wait: float = 20.0, client_id: str = "") -> list[dict]:
        if client_id and self.peer_id and self.peer_id != client_id and self.connected:
            raise ValueError("已有其他 AstrBot 实例接入当前 Ooptra，请先停用旧连接")
        if client_id:
            self.peer_id = client_id
        self._last_poll = time.monotonic()
        deadline = time.monotonic() + min(20, max(0, wait))
        while True:
            if self._closed:
                return []
            now = time.monotonic()
            events = list({event["request_id"]: event for event in
                           [item[0] for item in self._pending.values()] + list(self._outbox.values())}.values())
            ready = [event for event in events
                     if now - self._leased.get(event["request_id"], -100)
                     >= (25 if event["operation"] in {"bindings", "remember", "commit"} else 8)][:8]
            if ready:
                for event in ready:
                    self._leased[event["request_id"]] = now
                return [self._ledger.delivery_view(event) for event in ready]
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
                self._persist((request_id, result))
                pending[1].set_result({**copy.deepcopy(result), "queued": True, "request_id": request_id})
                self._error = str(result.get("error") or "记忆写入待重试")[:200]
                return True
            event = self._outbox.pop(request_id)
            try:
                self._persist((request_id, result))
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
                self._persist((request_id, result))
                self._error = str(result.get("error") or "记忆写入待重试")[:200]
                return True
            event = self._outbox.pop(request_id)
            try:
                self._persist((request_id, result))
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

    def publish_committed(self) -> None:
        """Publish only after the ledger transaction has committed."""
        if self._closed:
            return
        self._outbox = self._ledger.ready()
        self._wake.set()

    async def close(self) -> None:
        self._closed = True
        self.clear()
        for _, future in self._pending.values():
            if not future.done():
                future.set_result({"ok": False, "error": "上下文已关闭"})
        self._pending.clear()
        self._wake.set()
        if self._owns_ledger:
            self._ledger.close()

    async def poll_request(self, request: PollRequest) -> PollResult:
        if self._closed:
            return PollResult("closed")
        try:
            events = await self.poll(wait=request.wait, client_id=request.client_id)
        except ValueError:
            return PollResult("peer_conflict")
        return PollResult("events" if events else "closed" if self._closed else "timeout",
                          tuple(json.dumps(event, ensure_ascii=False) for event in events))

    def resolve_receipt(self, receipt: RpcReceipt) -> RpcResolution:
        if self._closed or not self.peer_id or receipt.client_id != self.peer_id:
            return RpcResolution("rejected")
        if receipt.request_id in self._resolved:
            return RpcResolution("duplicate")
        if self._ledger.delivery(receipt.request_id).state in {"confirmed", "failed"}:
            return RpcResolution("duplicate")
        result = json.loads(receipt.result_json)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            return RpcResolution("rejected")
        if not self.resolve(receipt.request_id, result):
            return RpcResolution("unknown")
        if result.get("retry") is not True:
            self._resolved.add(receipt.request_id)
            if len(self._resolved) > 2048:
                self._resolved.pop()
        return RpcResolution("accepted")

    async def bindings(self, request: BindingRequest) -> dict:
        return await self.request("bindings", json.loads(request.payload_json))
