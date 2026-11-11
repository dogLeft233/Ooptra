"""Context coordination: immutable preparation, scoped projection and atomic records."""
from __future__ import annotations

import asyncio
import copy
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from channels.models import RoomTarget
from context._internal.memory.ledger import ContextLedger, encoded
from context._internal.sources.bridge import ContextBridge
from context.models import (
    ConfirmedTurn,
    ContextRef,
    ContextRequest,
    ContextRevision,
    Observation,
    OutputRecord,
    PreparedContext,
    RecallRequest,
    RememberRequest,
    SituationSnapshot,
    WriteAuthorization,
)
from core.paths import project_path


class ContextService:
    def __init__(self, history_path: str, *, max_turns: int = 30, timeout: float = 4, outbox_path: Path | None = None):
        legacy = Path(history_path)
        if not legacy.is_absolute():
            legacy = Path(project_path(history_path))
        self._ledger = ContextLedger(legacy.with_name(legacy.stem + "_context.sqlite3"), legacy_history=legacy,
                                     legacy_outbox=outbox_path, max_turns=max_turns)
        self.history = self._ledger
        self.bridge = ContextBridge(timeout=timeout, ledger=self._ledger)
        self._loop = None
        self._closed = False
        self._observations: dict[str, tuple[Observation, int]] = {}
        self._seen: dict[str, str] = {}
        self._sequences: dict[tuple[str, str], int] = {}
        self._version = 0
        self._retired_sessions: set[str] = set()
        self._source_generations: dict[str, int] = {}
        self._bindings: dict[ContextRef, dict[str, Any]] = {}
        self._source_snapshots: dict[ContextRef, dict[str, Any]] = {}
        self._inputs: dict[str, Observation] = {}
        self._grants: dict[str, WriteAuthorization] = {}

    def _revision(self):
        return ContextRevision(self._version, len(self._source_generations), self._ledger.revision())

    def observe(self, observation: Observation) -> ContextRevision:
        if self._closed:
            raise RuntimeError("上下文已关闭")
        fingerprint = encoded(asdict(observation))
        if observation.event_id in self._seen:
            if self._seen[observation.event_id] != fingerprint:
                raise ValueError("观察 ID 内容冲突")
            return self._revision()
        room = encoded(asdict(observation.session.room_target))
        key = (observation.connection_id, observation.session.session_id)
        previous = self._sequences.get(key, -1)
        if observation.sequence <= previous:
            return self._revision()
        current = self._observations.get(room)
        # Only explicit session observations can replace an active session. Delayed transcription remains history.
        active = (observation.session.session_id not in self._retired_sessions and
                  (current is None or current[0].session == observation.session or observation.kind == "session"))
        if observation.kind == "recognized" and observation.complete:
            source = self.bridge.context
            event = None
            if (observation.text.strip() and source.get("ok") is True and self.bridge._room == asdict(observation.session.room_target)
                    and ("memory_scopes" not in source or (source.get("memory_scopes") and source.get("provider")))):
                event = self.bridge._event("commit", {"user_text": observation.text[:4000],
                    "reply_text": "", "speaker": observation.user_key, "interrupted": False})
                event["request_id"] = "input-" + observation.input_ref
            self._ledger.commit_input(observation.input_ref, ConfirmedTurn(observation.text, "", "silent", observation.user_key,
                                       f"{observation.session.room_target.area}/{observation.session.room_target.channel}"), room=room, session=observation.session.session_id, event=event)
            self._inputs[observation.input_ref] = observation
            if len(self._inputs) > 4096:
                self._inputs.pop(next(iter(self._inputs)))
        self._seen[observation.event_id] = fingerprint
        self._sequences[key] = observation.sequence
        if active:
            self._version += 1
            projection = observation
            if current and current[0].session == observation.session:
                prior = current[0]
                projection = replace(observation,
                    members=observation.members if observation.kind == "members" else prior.members,
                    text=observation.text if observation.kind == "recognized" else prior.text,
                    complete=(observation.complete if observation.kind == "members" else
                              prior.complete and observation.complete and observation.sequence == previous + 1))
            elif current:
                self._retired_sessions.add(current[0].session.session_id)
            self._observations[room] = (projection, self._version)
        # Bounded dedup affects projection only; durable input idempotency survives process restart.
        if len(self._seen) > 4096:
            self._seen.pop(next(iter(self._seen)))
        return self._revision()

    def snapshot(self, scope: RoomTarget) -> SituationSnapshot:
        room = encoded(asdict(scope))
        current = self._observations.get(room)
        observation = current[0] if current else None
        return SituationSnapshot(scope, observation.session if observation else None, self._revision(),
                                 observation.members if observation else (), observation.complete if observation else False,
                                 observation.text if observation else "",
                                 self._ledger.scoped_history(room=room))

    async def prepare_context(self, request: ContextRequest) -> PreparedContext:
        if self._closed:
            raise RuntimeError("上下文已关闭")
        self._loop = asyncio.get_running_loop()
        # Capture before any await; source IDs and payloads must never follow a later global source.
        revision = self._revision()
        if request.known_revision is not None and request.known_revision != revision:
            raise ValueError("准备请求的上下文版本已过期")
        bridge_id = self.bridge.session_id
        source = copy.deepcopy(self.bridge.context)
        bridge_room = dict(self.bridge._room)
        matches = bridge_room == asdict(request.session.room_target)
        active = source.get("ok") is True and matches
        if active:
            generation = self._source_generations.setdefault(bridge_id, len(self._source_generations) + 1)
            ref = ContextRef(request.session, generation, bridge_id)
            self._source_snapshots[ref] = source
            self._bindings[ref] = {"scope": {k: copy.deepcopy(source.get(k, "")) for k in
                                  ("group_id", "umo", "persona_id", "memory_persona_id", "provider", "platform_id", "bot_id", "source_kind", "memory_scopes", "memory_route_id")},
                                  "room": asdict(request.session.room_target), "session_id": bridge_id}
        else:
            ref = ContextRef(request.session, 0, "local")
        room = encoded(asdict(request.session.room_target))
        history = self._ledger.scoped_history(room=room, source=ref.binding_id, user_key=request.user_key,
                                              exclude_input_ref=request.input_ref)
        self._grants = {key: grant for key, grant in self._grants.items() if grant.expires_at >= time.time()}
        grants: tuple[WriteAuthorization, ...] = ()
        observed = self._inputs.get(request.input_ref)
        if active and observed and observed.complete and observed.session == request.session and observed.user_key and self._explicit_remember(observed.text):
            grant = WriteAuthorization(uuid.uuid4().hex, ref, request.input_ref, time.time() + 120)
            self._grants[grant.grant_id] = grant
            grants = (grant,)
        return PreparedContext(ref, revision, str(source.get("persona") or request.persona or self.history.get_persona()) if active else (request.persona or self.history.get_persona()),
                               history, grants, "ready" if active else "local")

    @staticmethod
    def _explicit_remember(text):
        return any(marker in text for marker in ("请记住", "帮我记住", "记住这", "记住我", "please remember"))

    def record_output(self, record: OutputRecord):
        if self._closed:
            raise RuntimeError("上下文已关闭")
        binding = self._bindings.get(record.context_ref) if record.context_ref else None
        event = None
        if binding and record.turn.user_text.strip():
            scope = binding["scope"]
            if "memory_scopes" not in scope or (scope["memory_scopes"] and scope.get("provider")):
                event = {"protocol": 1, "request_id": "record-" + record.action_id, **copy.deepcopy(binding),
                         "operation": "commit", "created_at": time.time(), "payload": {
                             "user_text": record.turn.user_text[:4000], "reply_text": record.turn.reply_text[:4000],
                             "speaker": record.turn.user_key, "interrupted": record.turn.confirmation == "interrupted"}}
        receipt = self._ledger.commit("action:" + record.action_id, asdict(record), record.turn,
                                      room=encoded(asdict(record.session.room_target)), session=record.session.session_id,
                                      source=record.context_ref.binding_id if record.context_ref else "",
                                      input_ref=record.input_ref, event=event,
                                      input_event_ref=record.input_ref if record.turn.user_text.strip() else "")
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.bridge.publish_committed)
        else:
            self.bridge.publish_committed()
        return receipt

    def finish_input(self, input_ref: str):
        """无输出终态采集原完整输入；源在observe时绑定，与输出共享耐久投递身份。"""
        receipt = self._ledger.finish_input(input_ref)
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.bridge.publish_committed)
        else:
            self.bridge.publish_committed()
        return receipt

    def delivery(self, delivery_id):
        return self._ledger.delivery(delivery_id)

    def _validate_ref(self, ref):
        if self._closed or ref not in self._bindings or ref.binding_id != self.bridge.session_id:
            raise ValueError("上下文来源已失效")
        return self._bindings[ref]

    async def recall(self, request: RecallRequest):
        self._validate_ref(request.context_ref)
        if not request.tool_call_id or not 1 <= len(request.query.strip()) <= 2000:
            raise ValueError("记忆查询需要调用 ID 和 1-2000 字内容")
        result = await self.bridge.request("recall", {"query": request.query, "speaker": request.speaker})
        self._validate_ref(request.context_ref)
        return result

    async def remember(self, request: RememberRequest):
        self._validate_ref(request.context_ref)
        grant = self._grants.get(request.authorization.grant_id)
        if (grant is None or grant != request.authorization or grant.context_ref != request.context_ref or grant.expires_at < time.time()
                or not request.tool_call_id or not 1 <= len(request.content.strip()) <= 2000):
            raise ValueError("记忆写入没有有效授权")
        binding = self._bindings[request.context_ref]
        event = {"protocol": 1, "request_id": "tool-" + request.tool_call_id, **copy.deepcopy(binding),
                 "operation": "remember", "created_at": grant.expires_at - 120,
                 "payload": {"content": request.content, "speaker": request.speaker}}
        receipt = self._ledger.commit("tool:" + request.tool_call_id,
                                      {"content": request.content, "context_ref": asdict(request.context_ref), "grant": grant.grant_id},
                                      ConfirmedTurn("", "", "silent"), event=event)
        self.bridge.publish_committed()
        return {"ok": False, "queued": receipt.delivery_admission == "accepted", "request_id": receipt.delivery_id,
                "error": "等待外部记忆确认" if receipt.delivery_admission == "accepted" else "待写队列已满"}

    def tools(self, prepared: PreparedContext):
        return BoundTools(self, prepared)

    async def export_legacy(self, directory: Path):
        return await asyncio.to_thread(self._ledger.export_legacy, directory)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await self.bridge.close()
        # Transactions hold a finite local lock; closing cannot drop a committed record.
        await asyncio.to_thread(self._ledger.close)


class BoundTools:
    def __init__(self, service, prepared):
        self._service, self._prepared = service, prepared

    @property
    def active(self):
        try:
            self._service._validate_ref(self._prepared.context_ref)
            return True
        except ValueError:
            return False

    @property
    def persona(self):
        return self._prepared.persona

    @property
    def context(self):
        return copy.deepcopy(self._service._source_snapshots.get(self._prepared.context_ref, {}))

    @property
    def history(self):
        return [asdict(row) for row in self._prepared.history]

    @property
    def session_id(self):
        return self._prepared.context_ref.binding_id

    async def recall(self, request: RecallRequest):
        if request.context_ref != self._prepared.context_ref:
            raise ValueError("工具请求不属于准备上下文")
        return await self._service.recall(request)

    async def remember(self, request: RememberRequest):
        if request.context_ref != self._prepared.context_ref or request.authorization not in self._prepared.authorized_write_grants:
            raise ValueError("工具请求不属于准备授权")
        return await self._service.remember(request)

    async def tool(self, name, args, *, speaker=""):
        if not isinstance(args, dict):
            return {"ok": False, "error": "记忆工具参数不正确"}
        try:
            call_id = str(args.get("tool_call_id") or uuid.uuid4().hex)
            if name == "astrbot_recall":
                return await self._service.recall(RecallRequest(call_id, self._prepared.context_ref, args.get("query", ""), speaker))
            if name == "astrbot_remember" and self._prepared.authorized_write_grants:
                return await self._service.remember(RememberRequest(call_id, self._prepared.context_ref, args.get("content", ""),
                                                     self._prepared.authorized_write_grants[0], speaker))
        except ValueError:
            return {"ok": False, "error": "记忆工具授权或来源无效"}
        return {"ok": False, "error": "未获记忆写入授权"}
