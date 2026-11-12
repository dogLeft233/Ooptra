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

    def commit(self, key: str, payload: Any, turn: ConfirmedTurn, *, room="", session="", source="", input_ref="", event=None, defer_event=False, input_event_ref="") -> RecordReceipt:
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
            if input_event_ref and not prior:
                original = db.execute("SELECT event FROM idempotency WHERE key=?",
                                      ("observation:" + input_event_ref,)).fetchone()
                if original is not None:
                    event = json.loads(original[0]) if original[0] else None
                    if event:
                        event["payload"]["reply_text"] = turn.reply_text[:4000]
                        event["payload"]["interrupted"] = turn.confirmation == "interrupted"
                        # 首个终态拥有原输入的外部采集；已领取/确认的载荷永不改写或再投。
                        if db.execute("SELECT 1 FROM deliveries WHERE id=?", (event["request_id"],)).fetchone():
                            event = None
            if event and not defer_event:
                self._validate_event(event)
                count = db.execute("SELECT COUNT(*) FROM deliveries WHERE state IN ('pending','retrying')").fetchone()[0]
                if count >= 128:
                    receipt = RecordReceipt(receipt.input_recorded, receipt.output_recorded, None, "rejected_capacity")
                else:
                    db.execute("INSERT OR IGNORE INTO deliveries(id,event,state) VALUES (?,?,'pending')", (event["request_id"], encoded(event)))
                    receipt = RecordReceipt(receipt.input_recorded, receipt.output_recorded, event["request_id"], "accepted")
            db.execute("INSERT OR REPLACE INTO idempotency VALUES (?,?,?,?)", (key, fingerprint, encoded(asdict(receipt)), encoded(event) if event else None))
            return receipt

    def commit_input(self, identifier, turn, *, room, session, event=None):
        return self.commit("observation:" + identifier, asdict(turn), turn, room=room, session=session,
                           input_ref=identifier, event=event, defer_event=True)

    def revoke_input_identity(self, identifier):
        with self.transaction() as db:
            db.execute("UPDATE history SET user_key='' WHERE entry_id=?", ("input:" + identifier,))
            row = db.execute("SELECT payload,event FROM idempotency WHERE key=?", ("observation:" + identifier,)).fetchone()
            if row:
                payload = json.loads(row[0])
                payload["user_key"] = ""
                event = json.loads(row[1]) if row[1] else None
                if event:
                    event["payload"]["speaker"] = ""
                db.execute("UPDATE idempotency SET payload=?,event=? WHERE key=?",
                    (encoded(payload), encoded(event) if event else None, "observation:" + identifier))

    def finish_input(self, identifier):
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM idempotency WHERE key=?", ("observation:" + identifier,)).fetchone():
                return RecordReceipt(False, False)
        return self.commit("input-delivery:" + identifier, {"input_ref": identifier},
                           ConfirmedTurn("", "", "silent"), input_event_ref=identifier)

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

    def scoped_history(self, *, room: str, source: str = "", user_key: str = "", limit=None, exclude_input_ref: str = ""):
        with self.transaction() as db:
            # Imported legacy entries have no trustworthy room and remain available only in the explicit legacy view.
            excluded = "input:" + exclude_input_ref if exclude_input_ref else ""
            rows = db.execute("SELECT role,content FROM history WHERE room=? AND (?='' OR source='' OR source=?) AND (?='' OR user_key='' OR user_key=?) AND (?='' OR entry_id<>?) ORDER BY seq DESC LIMIT ?", (room, source, source, user_key, user_key, excluded, excluded, limit or self.max_turns)).fetchall()
        return tuple(HistoryMessage(role, content) for role, content in reversed(rows))

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
