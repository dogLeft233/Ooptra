"""共享对话记忆：JSONL 落盘，供 Voice Agent 与外部插件读写。"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict
from typing import Any

from context.models import ConfirmedTurn, MemoryTurn, RecordReceipt
from core.paths import project_path


class MemoryStore:
    def __init__(self, path: str, max_turns: int = 30) -> None:
        self._path = path if os.path.isabs(path) else project_path(path)
        self._max_turns = max(1, int(max_turns))
        self._lock = threading.Lock()
        self._persona: str = ""
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)

    @property
    def path(self) -> str:
        return self._path

    def set_persona(self, persona: str) -> None:
        with self._lock:
            self._persona = str(persona or "")

    def get_persona(self) -> str:
        with self._lock:
            return self._persona

    def append(self, role: str, content: str, *, user_key: str = "", channel_key: str = "") -> dict[str, Any]:
        turn = MemoryTurn(
            role=role,
            content=str(content or ""),
            user_key=str(user_key or ""),
            channel_key=str(channel_key or ""),
            ts=time.time(),
        )
        line = json.dumps(asdict(turn), ensure_ascii=False)
        self._append_lines(line + "\n")
        return asdict(turn)

    def _append_lines(self, text: str) -> None:
        with self._lock:
            needs_separator = False
            if os.path.exists(self._path) and os.path.getsize(self._path):
                with open(self._path, "rb") as existing:
                    existing.seek(-1, os.SEEK_END)
                    needs_separator = existing.read(1) != b"\n"
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(("\n" if needs_separator else "") + text)

    def record(self, turn: ConfirmedTurn) -> RecordReceipt:
        rows: list[MemoryTurn] = []
        timestamp = time.time()
        if turn.user_text.strip():
            rows.append(MemoryTurn("user", turn.user_text, turn.user_key, turn.channel_key, timestamp))
        if turn.confirmation == "completed":
            rows.append(MemoryTurn("assistant", turn.reply_text, turn.user_key, turn.channel_key, timestamp))
        if rows:
            self._append_lines("".join(json.dumps(asdict(row), ensure_ascii=False) + "\n" for row in rows))
        return RecordReceipt(bool(turn.user_text.strip()), turn.confirmation == "completed")

    def recent(self, *, user_key: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        limit = self._max_turns if limit is None else max(1, int(limit))
        rows: deque[dict[str, Any]] = deque(maxlen=limit)
        if not os.path.exists(self._path):
            return []
        with self._lock, open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = MemoryTurn.parse(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if parsed is None:
                    continue
                row = asdict(parsed)
                if user_key and row.get("user_key") and row.get("user_key") != user_key:
                    continue
                rows.append(row)
        return list(rows)

    def as_messages(self, *, user_key: str = "", limit: int | None = None) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for row in self.recent(user_key=user_key, limit=limit):
            role = str(row.get("role") or "user")
            if role not in {"user", "assistant", "system"}:
                role = "user"
            out.append({"role": role, "content": str(row.get("content") or "")})
        return out

    def clear(self, *, user_key: str = "") -> int:
        """清空记忆：``user_key`` 为空 = 清掉**所有**行。

        WebUI 的「清空共享记忆」发的就是空 user_key，所以这里必须把空值当
        「全清」。早期写成 ``if user_key and row.get("user_key") == user_key``，
        空 key 下这个条件恒假 —— 一行都删不掉，返回 0，前端却照样弹「已清空」，
        看起来就是「按钮点了没反应」。注意别照抄 ``recent()`` 里那个守卫：
        那边的空值语义是「不做过滤」，这边是「全部命中」，方向正好相反。
        """
        if not os.path.exists(self._path):
            return 0
        removed = 0
        kept: list[str] = []
        with self._lock:
            with open(self._path, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        parsed = MemoryTurn.parse(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if parsed is None:
                        continue
                    if not user_key or parsed.user_key == user_key:
                        removed += 1
                        continue
                    kept.append(line if line.endswith("\n") else line + "\n")
            with open(self._path, "w", encoding="utf-8") as fh:
                fh.writelines(kept)
        return removed
