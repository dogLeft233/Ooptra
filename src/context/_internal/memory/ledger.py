"""SQLite owns history, idempotency, original delivery payloads and receipts."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from context.models import (
    ConfirmedTurn,
    DeliveryStatus,
    HistoryMessage,
    LegacyExport,
    MemoryTurn,
    RecordReceipt,
    VoiceTrace,
    VoiceTraceEntry,
    VoiceTracePage,
    VoiceTraceQuery,
)


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ContextLedger:
    def __init__(self, path: Path | str, *, legacy_history: Path | None = None, legacy_outbox: Path | None = None, max_turns: int = 30, strict_import: bool = True):
        self.path = str(path)
        self._memory_db = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        self.max_turns = max(1, int(max_turns))
        self._lock = threading.RLock()
        self._persona = ""
        self._strict_import = strict_import
        self._closed = False
        if self._memory_db is None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "context_metadata" not in tables:
                raise ValueError("上下文数据库不是 Ooptra 台账；未修改")
            db.execute("CREATE TABLE IF NOT EXISTS context_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            version = db.execute("SELECT value FROM context_metadata WHERE key='schema'").fetchone()
            if version and version[0] != "1":
                raise ValueError("上下文数据库版本不受支持")
            db.execute("INSERT OR IGNORE INTO context_metadata VALUES ('schema','1')")
            db.execute("PRAGMA user_version=1")
            db.execute("CREATE TABLE IF NOT EXISTS history (seq INTEGER PRIMARY KEY, entry_id TEXT UNIQUE NOT NULL, room TEXT NOT NULL, session TEXT NOT NULL, source TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, user_key TEXT NOT NULL, channel_key TEXT NOT NULL, ts REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, payload TEXT NOT NULL, receipt TEXT NOT NULL, event TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, event TEXT NOT NULL, state TEXT NOT NULL, receipts TEXT NOT NULL DEFAULT '{}')")
            db.execute("CREATE TABLE IF NOT EXISTS migration_state (source TEXT PRIMARY KEY, count INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS history_scope ON history(room,source,seq)")
            db.execute("CREATE TABLE IF NOT EXISTS voice_traces (seq INTEGER PRIMARY KEY AUTOINCREMENT, trace_key TEXT UNIQUE NOT NULL, area TEXT NOT NULL, channel TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS voice_trace_scope ON voice_traces(area,channel,seq)")
            for source, kind in ((legacy_history, "history"), (legacy_outbox, "outbox")):
                if source is not None:
                    self._import(db, source, kind)

    @contextmanager
    def transaction(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("上下文台账已关闭")
            db = self._memory_db or sqlite3.connect(self.path, timeout=10)
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                if self._memory_db is None:
                    db.close()

    def record_voice_trace(self, trace: VoiceTrace) -> None:
        key = encoded([asdict(trace.session), trace.input_ref])
        payload = asdict(trace)
        ranks = {"recognizing": 0, "recognized": 1, "deciding": 2, "finished": 3}
        now = time.time()
        with self.transaction() as db:
            prior = db.execute("SELECT payload FROM voice_traces WHERE trace_key=?", (key,)).fetchone()
            if prior:
                old = json.loads(prior[0])
                if old["user_key"] and trace.user_key and old["user_key"] != trace.user_key:
                    raise ValueError("语音诊断不能更换原说话者")
                payload["user_key"] = old["user_key"] or trace.user_key
                for name in ("text", "decision", "reason", "outcome", "recognition_status"):
                    if not payload[name]:
                        payload[name] = old.get(name, "")
                if old.get("recognition_status", "") not in {"", "pending"} and trace.recognition_status in {"", "pending", "interrupted"}:
                    payload["recognition_status"] = old["recognition_status"]
                if (old["text_final"] and old["text"]) or not trace.text:
                    payload["text"], payload["text_final"] = old["text"], old["text_final"]
                if ranks[trace.stage] < ranks[old["stage"]]:
                    for name in ("stage", "decision", "reason", "outcome", "elapsed_ms"):
                        payload[name] = old[name]
                db.execute("UPDATE voice_traces SET payload=?,updated_at=? WHERE trace_key=?", (encoded(payload), now, key))
            else:
                db.execute("INSERT INTO voice_traces(trace_key,area,channel,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           (key, trace.session.room_target.area, trace.session.room_target.channel, encoded(payload), now, now))

    def voice_traces(self, query: VoiceTraceQuery) -> VoiceTracePage:
        from channels.models import RoomTarget, SessionRef

        clauses, args = [], []
        if query.scope is not None:
            clauses.append("area=? AND channel=?")
            args.extend((query.scope.area, query.scope.channel))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.transaction() as db:
            total = db.execute("SELECT COUNT(*) FROM voice_traces" + where, args).fetchone()[0]
            if query.before is not None:
                clauses.append("seq<?")
                args.append(query.before)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            records = db.execute("SELECT seq,payload,created_at,updated_at FROM voice_traces" + where + " ORDER BY seq DESC LIMIT ?", [*args, query.limit+1]).fetchall()
        result = []
        for seq, raw, created, updated in records[:query.limit]:
            fields = json.loads(raw)
            session = fields.pop("session")
            target = RoomTarget(**session.pop("room_target"))
            trace = VoiceTrace(session=SessionRef(room_target=target, **session), **fields)
            result.append(VoiceTraceEntry(seq, created, updated, trace))
        return VoiceTracePage(tuple(result), result[-1].sequence if len(records) > query.limit else None, total)

    def _import(self, db, path: Path, kind: str):
        source = kind + ":" + str(path.resolve())
        if db.execute("SELECT 1 FROM migration_state WHERE source=?", (source,)).fetchone():
            return
        rows: list[Any] = []
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if kind == "outbox":
                rows = json.loads(text)
                if not isinstance(rows, list) or len(rows) > 128:
                    raise ValueError("旧待写队列格式不合法；未切换上下文存储")
            else:
                for line in text.splitlines():
                    if line.strip():
                        try:
                            value = MemoryTurn.parse(json.loads(line))
                        except json.JSONDecodeError:
                            if self._strict_import:
                                raise ValueError("旧历史格式不合法；未切换上下文存储") from None
                            continue
                        if value is None:
                            if self._strict_import:
                                raise ValueError("旧历史格式不合法；未切换上下文存储")
                            continue
                        rows.append(asdict(value))
        for index, row in enumerate(rows):
            if kind == "history":
                identifier = "import:" + hashlib.sha256((source + str(index)).encode()).hexdigest()
                self._insert_history(db, identifier, row, "", "", "")
            else:
                self._validate_event(row)
                prior = db.execute("SELECT event FROM deliveries WHERE id=?", (row["request_id"],)).fetchone()
                if prior and prior[0] != encoded(row):
                    raise ValueError("旧队列重复请求载荷冲突")
                db.execute("INSERT OR IGNORE INTO deliveries(id,event,state) VALUES (?,?,'pending')", (row["request_id"], encoded(row)))
        db.execute("INSERT INTO migration_state VALUES (?,?)", (source, len(rows)))

    @staticmethod
    def _validate_event(event):
        if (not isinstance(event, dict) or event.get("operation") not in {"commit", "remember"}
                or not isinstance(event.get("request_id"), str) or not event["request_id"]
                or not isinstance(event.get("session_id"), str)
                or not all(isinstance(event.get(k), dict) for k in ("room", "scope", "payload"))):
            raise ValueError("待写队列含不合法的耐久投递事件")

    @staticmethod
    def _insert_history(db, identifier, row, room, session, source):
        db.execute("INSERT OR IGNORE INTO history(entry_id,room,session,source,role,content,user_key,channel_key,ts) VALUES (?,?,?,?,?,?,?,?,?)", (identifier, room, session, source, row["role"], row["content"], row.get("user_key", ""), row.get("channel_key", ""), row.get("ts", time.time())))

    def commit(self, key: str, payload: Any, turn: ConfirmedTurn, *, room="", session="", source="", input_ref="", event=None) -> RecordReceipt:
        fingerprint = encoded(payload)
        with self.transaction() as db:
            prior = db.execute("SELECT payload,receipt,event FROM idempotency WHERE key=?", (key,)).fetchone()
            if prior and prior[0] != fingerprint:
                raise ValueError("同一记录 ID 的内容冲突")
            if prior:
                receipt = RecordReceipt(**json.loads(prior[1]))
                event = json.loads(prior[2]) if prior[2] else None
                if receipt.delivery_admission != "rejected_capacity":
                    return receipt
            else:
                if turn.user_text.strip():
                    self._insert_history(db, "input:" + (input_ref or key), asdict(MemoryTurn("user", turn.user_text, turn.user_key, turn.channel_key, time.time())), room, session, source)
                if turn.confirmation == "completed":
                    self._insert_history(db, "output:" + key, asdict(MemoryTurn("assistant", turn.reply_text, turn.user_key, turn.channel_key, time.time())), room, session, source)
                receipt = RecordReceipt(bool(turn.user_text.strip()), turn.confirmation == "completed")
            if event:
                self._validate_event(event)
                count = db.execute("SELECT COUNT(*) FROM deliveries WHERE state IN ('pending','retrying')").fetchone()[0]
                if count >= 128:
                    receipt = RecordReceipt(receipt.input_recorded, receipt.output_recorded, None, "rejected_capacity")
                else:
                    db.execute("INSERT OR IGNORE INTO deliveries(id,event,state) VALUES (?,?,'pending')", (event["request_id"], encoded(event)))
                    receipt = RecordReceipt(receipt.input_recorded, receipt.output_recorded, event["request_id"], "accepted")
            db.execute("INSERT OR REPLACE INTO idempotency VALUES (?,?,?,?)", (key, fingerprint, encoded(asdict(receipt)), encoded(event) if event else None))
            return receipt

    def commit_input(self, identifier, turn, *, room, session):
        return self.commit("observation:" + identifier, asdict(turn), turn, room=room, session=session, input_ref=identifier)

    def ready(self):
        with self.transaction() as db:
            return {row[0]: json.loads(row[1]) for row in db.execute("SELECT id,event FROM deliveries WHERE state IN ('pending','retrying') ORDER BY rowid")}

    def replace_pending(self, events, terminal=None):
        with self.transaction() as db:
            for event in events.values():
                self._validate_event(event)
                prior = db.execute("SELECT event FROM deliveries WHERE id=?", (event["request_id"],)).fetchone()
                if prior and prior[0] != encoded(event):
                    raise ValueError("投递 ID 载荷冲突")
                db.execute("INSERT OR IGNORE INTO deliveries(id,event,state) VALUES (?,?,'pending')", (event["request_id"], encoded(event)))
            if terminal is not None:
                identifier, result = terminal
                state = "retrying" if result.get("retry") is True else "confirmed" if result.get("ok") is True else "failed"
                receipts = result.get("targets", result.get("target_results", {}))
                previous = db.execute("SELECT receipts FROM deliveries WHERE id=?", (identifier,)).fetchone()
                old = json.loads(previous[0]) if previous else {}
                if isinstance(receipts, list):
                    combined = {str(row.get("umo", index)): row for index, row in enumerate(old if isinstance(old, list) else []) if isinstance(row, dict)}
                    for index, row in enumerate(receipts):
                        if not isinstance(row, dict):
                            continue
                        key = str(row.get("umo", index))
                        if combined.get(key, {}).get("ok") is not True:
                            combined[key] = row
                    receipts = list(combined.values())
                db.execute("UPDATE deliveries SET state=?,receipts=? WHERE id=?", (state, encoded(receipts), identifier))

    def delivery_view(self, event):
        """Keep canonical payload immutable; only transport to unconfirmed original targets."""
        with self.transaction() as db:
            row = db.execute("SELECT receipts FROM deliveries WHERE id=?", (event["request_id"],)).fetchone()
        receipts = json.loads(row[0]) if row else {}
        confirmed = {receipt.get("umo") for receipt in receipts if isinstance(receipt, dict) and receipt.get("ok") is True} if isinstance(receipts, list) else set()
        value = copy.deepcopy(event)
        scopes = value.get("scope", {}).get("memory_scopes")
        if isinstance(scopes, list):
            value["scope"]["memory_scopes"] = [scope for scope in scopes if scope.get("umo") not in confirmed]
        return value

    def delivery(self, identifier):
        with self.transaction() as db:
            row = db.execute("SELECT state,receipts FROM deliveries WHERE id=?", (identifier,)).fetchone()
        if not row:
            return DeliveryStatus(identifier, "unknown")
        receipts = json.loads(row[1])
        if isinstance(receipts, list):
            pairs = tuple((str(i), encoded(value)) for i, value in enumerate(receipts))
        else:
            pairs = tuple((str(k), encoded(v)) for k, v in receipts.items())
        return DeliveryStatus(identifier, row[0], pairs)

    def scoped_history(self, *, room: str, source: str = "", user_key: str = "", limit=None, exclude_input_ref: str = "", since: float = 0):
        with self.transaction() as db:
            # Imported legacy entries have no trustworthy room and remain available only in the explicit legacy view.
            excluded = "input:" + exclude_input_ref if exclude_input_ref else ""
            rows = db.execute("SELECT role,content,user_key,ts FROM history WHERE room=? AND (?='' OR source='' OR source=?) AND (?='' OR user_key='' OR user_key=?) AND (?='' OR entry_id<>?) AND ts>=? ORDER BY seq DESC LIMIT ?", (room, source, source, user_key, user_key, excluded, excluded, since, limit or self.max_turns)).fetchall()
        return tuple(HistoryMessage(role, content, user_key, ts) for role, content, user_key, ts in reversed(rows))

    def revision(self):
        with self.transaction() as db:
            return db.execute("SELECT COALESCE(MAX(seq),0) FROM history").fetchone()[0]

    def append(self, role, content, *, user_key="", channel_key=""):
        row = asdict(MemoryTurn(role, str(content or ""), user_key, channel_key, time.time()))
        with self.transaction() as db:
            self._insert_history(db, uuid.uuid4().hex, row, "", "", "")
        return row

    def record(self, turn):
        return self.commit(uuid.uuid4().hex, asdict(turn), turn)

    def recent(self, *, user_key: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        with self.transaction() as db:
            rows = db.execute("SELECT role,content,user_key,channel_key,ts FROM history WHERE (?='' OR user_key='' OR user_key=?) ORDER BY seq DESC LIMIT ?", (user_key, user_key, limit or self.max_turns)).fetchall()
        return [dict(zip(("role", "content", "user_key", "channel_key", "ts"), row, strict=True)) for row in reversed(rows)]

    def as_messages(self, *, user_key="", limit=None):
        return [{"role": row["role"], "content": row["content"]} for row in self.recent(user_key=user_key, limit=limit)]

    def clear(self, *, user_key=""):
        with self.transaction() as db:
            cursor = db.execute("DELETE FROM history WHERE (?='' OR user_key=?)", (user_key, user_key))
            return cursor.rowcount

    def set_persona(self, persona):
        self._persona = str(persona or "")

    def get_persona(self):
        return self._persona

    def export_legacy(self, directory: Path) -> LegacyExport:
        """Explicit offline downgrade export; originals and the database stay untouched.

        The manifest is published last and hashes both files. A directory without a matching
        manifest is incomplete and must not be used for restore.
        """
        directory = directory.resolve()
        if directory.exists() and any(directory.iterdir()):
            raise ValueError("旧格式导出必须使用空目录")
        directory.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            rows = db.execute("SELECT role,content,user_key,channel_key,ts FROM history ORDER BY seq").fetchall()
            events = [json.loads(row[0]) for row in db.execute("SELECT event FROM deliveries WHERE state IN ('pending','retrying') ORDER BY rowid")]
        history = "".join(encoded(dict(zip(("role", "content", "user_key", "channel_key", "ts"), row, strict=True))) + "\n" for row in rows)
        contents = {"voice_memory.jsonl": history, "astrbot_voice_outbox.json": encoded(events)}
        hashes = {}
        for name, content in contents.items():
            data = content.encode("utf-8")
            temporary = directory / (name + ".tmp")
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / name)
            hashes[name] = hashlib.sha256(data).hexdigest()
        manifest = directory / "context-export.json"
        temporary = manifest.with_suffix(".tmp")
        temporary.write_text(encoded({"schema": 1, "history_count": len(rows), "delivery_count": len(events), "sha256": hashes}), encoding="utf-8")
        os.replace(temporary, manifest)
        return LegacyExport(str(directory), len(rows), len(events), str(manifest))

    def close(self):
        with self._lock:
            self._closed = True
            if self._memory_db is not None:
                self._memory_db.close()
