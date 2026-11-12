"""Bounded auto visits and a shared empty-room guard for all voice rooms."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import uuid
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from behavior._internal.visits.settings import effective_area
from behavior.api import VisitsHost, VisitStore
from behavior.models import AutoVisitConfig
from channels.models import PlatformMember

logger = logging.getLogger(__name__)


class _Clock:
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(asyncio.sleep)

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(timezone.utc)


class AutoVisitController:
    def __init__(self, agent: VisitsHost, config: AutoVisitConfig, store: VisitStore,
                 *, enabled: Callable[[], bool], clock: Any = None, rng: Any = None) -> None:
        self.agent, self.store = agent, store
        self._config = deepcopy(config)
        self.enabled = enabled
        self.clock, self.rng = clock or _Clock(), rng or random.Random()
        self._ready = False
        self._paused = False
        self._pause_reason = ""
        self._phase = "waiting"
        self._last_action = "等待启用域与语音连接"
        self._last_error = ""
        self._state_error = ""
        self._loop: asyncio.Task | None = None
        self._visit_task: asyncio.Task | None = None
        self._wake, self._visit_wake = asyncio.Event(), asyncio.Event()
        self._check_lock, self._finish_lock = asyncio.Lock(), asyncio.Lock()
        self._empty_check_lock = asyncio.Lock()
        self._epoch = 0
        self._next_check: float | None = None
        self._leave_at: float | None = None
        self._empty_since: float | None = None
        self._empty_room: tuple[int, int, str, str] | None = None
        self._empty_state = "unknown"
        self._empty_checked_at: str | None = None
        self._empty_error = ""
        self._last_exit_reason = ""
        self._visit_id = ""
        self._visit_area = ""
        self._visit_channel = ""
        self._visit_policy: dict[str, Any] = {}
        self._cooldown_deadlines: dict[str, tuple[str, float]] = {}

    @property
    def config(self) -> AutoVisitConfig:
        """兼容管理面配置快照；修改只能通过 reload。"""
        return deepcopy(self._config)

    def _cooldown_deadline(self, key: str, value: str | None) -> float | None:
        if value is None:
            self._cooldown_deadlines.pop(key, None)
            return None
        cached = self._cooldown_deadlines.get(key)
        if cached is None or cached[0] != value:
            remaining = max(0, (datetime.fromisoformat(value) - self.clock.utcnow()).total_seconds())
            cached = value, self.clock.monotonic() + remaining
            self._cooldown_deadlines[key] = cached
        return cached[1]

    def _cooling(self, key: str, value: str | None) -> bool:
        deadline = self._cooldown_deadline(key, value)
        return deadline is not None and deadline > self.clock.monotonic()

    def _can_join(self, area: str) -> bool:
        snapshot = self._snapshot()
        return (not self._cooling("global", snapshot["global_cooldown_until"])
                and not self._cooling(f"area:{area}", snapshot["area_cooldowns"].get(area))
                and self.store.can_join(area, self.clock.utcnow(), self.config.daily_limit,
                                        effective_area(self.config, area)["daily_limit"],
                                        check_cooldowns=False))

    def _snapshot(self) -> dict[str, Any]:
        try:
            snapshot = self.store.snapshot(self.clock.utcnow())
            self._cooldown_deadline("global", snapshot["global_cooldown_until"])
            for area, until in snapshot["area_cooldowns"].items():
                self._cooldown_deadline(f"area:{area}", until)
            return snapshot
        except Exception as exc:
            self._fail_state(exc)
            return {"day": None, "daily_count": None, "area_counts": {},
                    "global_cooldown_until": None, "area_cooldowns": {}, "pending": {}}

    def _fail_state(self, exc: Exception) -> None:
        self._state_error = str(exc)
        self._paused = True
        self._pause_reason = "自动串门记录不可用，请检查状态记录后恢复"
        self._last_error = str(exc)
        self._phase = "paused"
        logger.warning("auto visit state unavailable: %s", exc)

    async def _write(self, method: str, *args: Any) -> None:
        try:
            await asyncio.to_thread(getattr(self.store, method), *args)
            if method in {"set_global_cooldown", "set_area_cooldown"}:
                self._snapshot()
        except Exception as exc:
            self._fail_state(exc)
            raise

    def _schedule_check(self) -> float:
        next_check = self.clock.monotonic() + self.rng.uniform(
            *self.config.check_interval_minutes) * 60
        self._next_check = next_check
        return next_check

    def _utc_deadline(self, value: float | None) -> str | None:
        if value is None:
            return None
        return (self.clock.utcnow() + timedelta(
            seconds=max(0.0, value - self.clock.monotonic()))).isoformat()

    def status(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        room = self.agent.status()
        areas = {}
        for area in self.config.areas:
            policy = effective_area(self.config, area)
            areas[area] = {
                "enabled": policy["enabled"], "daily_limit": policy["daily_limit"],
                "daily_count": snapshot["area_counts"].get(area, 0),
                "manual_cooldown_until": self._utc_deadline(self._cooldown_deadline(
                    f"area:{area}", snapshot["area_cooldowns"].get(area))),
            }
        current_key = self._room_key()
        countdown = (max(0.0, 30 - (self.clock.monotonic() - self._empty_since))
                     if current_key is not None and current_key == self._empty_room
                     and self._empty_since is not None else None)
        return {"empty_room": {"state": (self._empty_state if current_key == self._empty_room else "unknown") if current_key else "inactive",
                               "remaining_seconds": countdown,
                               "checked_at": self._empty_checked_at,
                               "last_exit_reason": self._last_exit_reason,
                               "error": self._empty_error},
                "phase": "paused" if self._paused else self._phase,
                "paused": self._paused, "pause_reason": self._pause_reason,
                "next_check_at": self._utc_deadline(self._next_check),
                "leave_at": self._utc_deadline(self._leave_at),
                "global_cooldown_until": self._utc_deadline(self._cooldown_deadline(
                    "global", snapshot["global_cooldown_until"])),
                "day": snapshot["day"], "daily_count": snapshot["daily_count"],
                "daily_limit": self.config.daily_limit, "areas": areas,
                "last_action": self._last_action, "last_error": self._last_error,
                "join_source": room.get("join_source", ""),
                "current_area": room.get("area", "") if room.get("joined") else "",
                "current_channel": room.get("channel", "") if room.get("joined") else ""}

    async def start(self) -> None:
        if self._loop and not self._loop.done():
            return
        try:
            await asyncio.to_thread(self.store.load)
            if self._snapshot()["pending"]:
                raise ValueError("前次进房中断，存在未确认的记录，需要核对语音状态")
        except Exception as exc:
            self._fail_state(exc)
        self._schedule_check()
        self._loop = asyncio.create_task(self._run_loop(), name="voice-auto-visit")

    async def _cancel_visit(self) -> None:
        task, self._visit_task = self._visit_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def stop(self) -> None:
        self._epoch += 1
        task, self._loop = self._loop, None
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._cancel_visit()
        self._next_check = None
        self._leave_at = None
        self._empty_since = self._empty_room = None

    def set_bot_ready(self, ready: bool) -> None:
        if self._ready != ready:
            self._epoch += 1
        self._ready = ready
        if ready:
            self._schedule_check()
        else:
            self._next_check = None
        self._wake.set()
        self._visit_wake.set()

    def notify_presence(self, area: str, channel: str) -> None:
        room = self.agent.status()
        if room.get("joined") and area == room.get("area") and channel == room.get("channel"):
            # A member can enter and leave between polls; require a fresh quiet interval.
            self._empty_since = None
            self._empty_state = "unknown"
            self._empty_checked_at = None
            self._wake.set()
        if area == self._visit_area and channel == self._visit_channel:
            self._visit_wake.set()

    async def pause(self) -> None:
        self._epoch += 1
        self._paused = True
        self._pause_reason = "已手动暂停自动加入；当前自动停留仍按原规则结束"
        self._next_check = None
        self._wake.set()

    async def resume(self) -> None:
        await asyncio.to_thread(self.store.load)
        if self._snapshot()["pending"]:
            raise ValueError("前次进房存在未确认的记录，请先核对语音状态")
        self._state_error = ""
        self._paused, self._pause_reason = False, ""
        self._schedule_check()
        self._wake.set()

    async def reload(self, config: AutoVisitConfig) -> None:
        if config != self.config:
            self._epoch += 1
        changed_interval = config.check_interval_minutes != self.config.check_interval_minutes
        self._config = deepcopy(config)
        if changed_interval or self._next_check is None:
            self._schedule_check()
        self._wake.set()
        self._visit_wake.set()

    async def on_operation(self, event: Any) -> None:
        self._empty_since = self._empty_room = None
        if event.source == "manual":
            self._epoch += 1
            await self._cancel_visit()
            self._visit_id = ""
            self._leave_at = self._empty_since = None
            if event.kind == "leave" and event.area:
                policy = effective_area(self.config, event.area)
                seconds = self.rng.uniform(*policy["manual_cooldown_minutes"]) * 60
                try:
                    await self._write("set_area_cooldown", event.area,
                                      self.clock.utcnow() + timedelta(seconds=seconds))
                except Exception:
                    return
                self._last_action = "手动退房：该域进入较长冷却，其他域继续等待检查"
            else:
                self._last_action = "手动房间由你控制；独处 30 秒会自动退出"
            self._schedule_check()
            if event.kind == "join":
                # Manual API joins are possible even when voice/auto-visit switches are off.
                await self.start()
        elif event.source == "system":
            self._epoch += 1
            await self._cancel_visit()
            self._visit_id = ""
            self._leave_at = self._empty_since = None
        elif event.kind == "leave":
            await self._cancel_visit()
            self._visit_id = ""
            self._leave_at = None
        self._wake.set()

    async def _sleep_or_wake(self, seconds: float, *, visit: bool = False) -> None:
        event = self._visit_wake if visit else self._wake
        if event.is_set():
            event.clear()
            return
        tasks = [asyncio.create_task(self.clock.sleep(max(0.0, seconds))),
                 asyncio.create_task(event.wait())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            event.clear()

    def _blocked(self, snapshot: dict[str, Any]) -> str:
        if self._paused or self._state_error:
            return self._pause_reason or "自动串门记录不可用"
        if not self.enabled():
            return "语音对话未启用"
        if not self._ready:
            return "等待 Oopz 连接就绪"
        if self.agent.status().get("joined"):
            return "当前房间由用户或自动停留控制"
        if not any(row["enabled"] for row in self.config.areas.values()):
            return "尚未开启任何域"
        if snapshot["pending"]:
            return "前次进房存在未确认的记录"
        if self._cooling("global", snapshot["global_cooldown_until"]):
            return "自动退房后休息中"
        count = snapshot["daily_count"]
        if count is None:
            return "今日记录不可用"
        if self.config.daily_limit and count >= self.config.daily_limit:
            return "今日自动进房次数已用完"
        return ""

    async def _run_loop(self) -> None:
        task = asyncio.current_task()
        while self._loop is task:
            await self._check_empty_room()
            # Python 3.10 wait_for can consume cancellation as a query completes.
            if self._loop is not task:
                return
            snapshot = self._snapshot()
            blocked = self._blocked(snapshot)
            if blocked:
                self._next_check = None
                if not self._visit_id:
                    self._last_action = blocked
                    self._phase = "cooldown" if self._cooling(
                        "global", snapshot["global_cooldown_until"]) else "waiting"
                await self._sleep_or_wake(5 if self.agent.status().get("joined") else 60)
                continue
            next_check = self._next_check
            if next_check is None:
                next_check = self._schedule_check()
            delay = next_check - self.clock.monotonic()
            if delay > 0:
                self._phase = "waiting"
                await self._sleep_or_wake(delay)
                continue
            await self._check_once()
            self._schedule_check()

    async def _query_rooms(self, area: str, *, humans_only: bool = True) -> dict[str, list[PlatformMember]]:
        observed = await self.agent.directory.voice_members(area)
        if observed.status != "confirmed":
            raise RuntimeError(f"成员查询未确认：{observed.status}")
        return {
            room.target.channel: [member for member in room.members
                                  if not humans_only or (member.uid and not member.is_self
                                                         and member.is_bot is False)]
            for room in observed.rooms
        }

    async def _check_once(self) -> None:
        async with self._check_lock:
            snapshot = self._snapshot()
            if self._blocked(snapshot):
                return
            epoch = self._epoch
            directory = self.agent.directory
            self._phase = "checking"
            try:
                joined = await directory.joined_areas()
                if directory is not self.agent.directory or epoch != self._epoch:
                    return
                if joined.status != "confirmed":
                    raise RuntimeError(f"域查询未确认：{joined.status}")
                joined_ids = set(joined.area_ids)
                eligible = [area for area in sorted(self.config.areas)
                            if area in joined_ids and effective_area(self.config, area)["enabled"]
                            and self._can_join(area)]
                semaphore = asyncio.Semaphore(4)

                async def inspect(area: str) -> tuple[str, list[str]]:
                    async with semaphore:
                        try:
                            rooms = await self._query_rooms(area)
                            return area, sorted(cid for cid, people in rooms.items() if people)
                        except Exception as exc:
                            self._last_error = f"查询域 {area} 失败：{exc}"
                            return area, []

                candidates = {area: rooms for area, rooms in await asyncio.gather(
                    *(inspect(area) for area in eligible)) if rooms}
                if not candidates:
                    self._last_action = "本轮没有可加入的真人房间"
                    return
                area = self.rng.choice(list(candidates))
                policy = effective_area(self.config, area)
                if self.rng.random() >= policy["join_probability"]:
                    self._last_action = "本轮随机决定暂不串门"
                    return
                channel = self.rng.choice(candidates[area])
                fresh_rooms = await self._query_rooms(area)
                if (not fresh_rooms.get(channel) or directory is not self.agent.directory
                        or epoch != self._epoch
                        or self._blocked(self._snapshot())
                        or not effective_area(self.config, area)["enabled"]
                        or not self._can_join(area)):
                    return
                visit_id = uuid.uuid4().hex
                operation_epoch = self.agent.operation_epoch
                await self._write("reserve", visit_id, area, self.clock.utcnow())
                if (directory is not self.agent.directory or epoch != self._epoch
                        or self._paused or not self._ready
                        or not self.enabled()
                        or not effective_area(self.config, area)["enabled"]
                        or self.agent.status().get("joined")):
                    await self._write("rollback", visit_id)
                    return
                self._visit_id, self._visit_area, self._visit_channel = visit_id, area, channel
                self._phase = "joining"
                try:
                    result = await self.agent.join(area, channel, source="auto", visit_id=visit_id,
                                                   expected_operation_epoch=operation_epoch)
                    if not result.get("ok", True):
                        raise RuntimeError(result.get("error", "进房未成功"))
                except asyncio.CancelledError:
                    # Preserve uncertain admissions; never silently erase possible success.
                    raise
                except Exception:
                    # A transport timeout may leave Oopz admission unresolved.
                    # Only explicit idle with no retired transport is proof that
                    # releasing this durable quota reservation is safe.
                    try:
                        room = self.agent.status()
                    except Exception:
                        room = {}
                    if room.get("room_state") == "idle" and room.get("room_cleanup_pending") is False:
                        await self._write("rollback", visit_id)
                        self._visit_id = ""
                    else:
                        self._paused = True
                        self._pause_reason = "进房结果未确认，已保留访问预留；请先核对渠道状态"
                        self._next_check = None
                    raise
                # A joined room needs a retirement owner even when persisting confirmation fails.
                if self.agent.is_auto_visit_current(visit_id):
                    self._visit_policy = policy
                    self._leave_at = self.clock.monotonic() + self.rng.uniform(*policy["stay_minutes"]) * 60
                    self._empty_since = None
                    self._last_action = "自动进房成功"
                    self._visit_task = asyncio.create_task(self._run_visit(visit_id), name="auto-visit-room")
                await self._write("confirm", visit_id, area, self.clock.utcnow())
            except Exception as exc:
                self._last_error = str(exc)
                self._last_action = "本轮未能完成进房，等待下一次检查"
                logger.warning("auto visit check failed: %s", exc)
            finally:
                if not self._visit_task or self._visit_task.done():
                    self._phase = "waiting"

    async def _say(self, kind: Literal["enter", "leave"], visit_id: str) -> None:
        policy = effective_area(self.config, self._visit_area)
        prompts = policy[f"{kind}_prompts"]
        if not prompts or not self.agent.is_auto_visit_current(visit_id):
            return
        result = await self.agent.speak_announcement(kind, self.rng.choice(prompts),
                                                    expected_visit_id=visit_id, timeout=20)
        if not result.get("ok"):
            self._last_error = result.get("error", "预设语音未能完成")

    async def _run_visit(self, visit_id: str) -> None:
        try:
            self._phase = "greeting"
            try:
                await self._say("enter", visit_id)
            except Exception as exc:
                self._last_error = str(exc)
            while self.agent.is_auto_visit_current(visit_id):
                if not self._ready:
                    await self._finish_visit("disconnect")
                    return
                if not effective_area(self.config, self._visit_area)["enabled"]:
                    await self._finish_visit("disabled")
                    return
                if self._leave_at is not None and self.clock.monotonic() >= self._leave_at:
                    await self._finish_visit("scheduled")
                    return
                self._phase = "active"
                delay = max(0, min(60, (self._leave_at or self.clock.monotonic() + 60)
                                    - self.clock.monotonic()))
                await self._sleep_or_wake(delay, visit=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = str(exc)
            # Failed greetings must not remove the bounded visit deadline.
            if self.agent.is_auto_visit_current(visit_id):
                await self._finish_visit("speech_error")

    def _room_key(self) -> tuple[int, int, str, str] | None:
        room = self.agent.status()
        if not self._ready or not room.get("joined"):
            return None
        return (id(self.agent.directory), self.agent.operation_epoch,
                room.get("area", ""), room.get("channel", ""))

    async def _room_empty_confirmed(self) -> bool:
        key = self._room_key()
        if key != self._empty_room:
            self._empty_room, self._empty_since = key, None
            self._empty_state, self._empty_error = "unknown", ""
        if key is None:
            self._empty_since = None
            return False

        async def only_self() -> bool:
            # Include other bots: "no humans" is not the same as "only this bot".
            rooms = await self._query_rooms(key[2], humans_only=False)
            members = rooms.get(key[3], [])
            return len(members) == 1 and members[0].is_self

        try:
            alone = await only_self()
            self._empty_checked_at = self.clock.utcnow().isoformat()
            self._empty_error = ""
        except Exception:
            self._empty_since = None
            self._empty_state = "unknown"
            self._empty_error = "无法确认成员状态，已重置无人倒计时"
            return False
        if not alone or key != self._room_key():
            self._empty_since = None
            self._empty_state = "occupied" if key == self._room_key() else "unknown"
            return False
        self._empty_state = "alone"
        now = self.clock.monotonic()
        empty_since = self._empty_since
        if empty_since is None:
            empty_since = now
            self._empty_since = empty_since
        if now - empty_since < 30:
            return False
        try:
            verified = await only_self()
        except Exception:
            self._empty_since = None
            self._empty_state = "unknown"
            self._empty_error = "退出前复查失败，已重置无人倒计时"
            return False
        if not verified or key != self._room_key():
            self._empty_since = None
            self._empty_state = "occupied" if key == self._room_key() else "unknown"
            return False
        return self._empty_since is not None and self.clock.monotonic() - self._empty_since >= 30

    async def _check_empty_room(self) -> None:
        async with self._empty_check_lock:
            key = self._room_key()
            confirmed = await self._room_empty_confirmed()
            if key is None or not confirmed or key != self._room_key():
                return
            try:
                source = "auto" if self.agent.status().get("join_source") == "auto" else "manual"
                # Empty exits bypass farewell waits and short retries, regardless of ownership.
                result = await self.agent.leave(source=source, expected_operation_epoch=key[1])
                if not result.get("ok", False):
                    raise RuntimeError(result.get("error", "无人退房未成功"))
                if source == "auto":
                    await self._cooldown_after_auto_exit(key[2])
                self._last_action = "房间只剩 bot 持续 30 秒，已静默退出"
                self._last_exit_reason = "alone_30_seconds"
                self._empty_error = ""
            except Exception as exc:
                self._last_error = str(exc)
                self._empty_error = "无人退房未成功，将重新确认独处状态后重试"
                logger.warning("empty room leave failed: %s", exc)
            finally:
                # A failed leave waits another confirmed interval before retrying.
                self._empty_since = None

    async def _cooldown_after_auto_exit(self, area: str) -> None:
        seconds = self.rng.uniform(*effective_area(self.config, area)["auto_cooldown_minutes"]) * 60
        await self._write("set_global_cooldown", self.clock.utcnow() + timedelta(seconds=seconds))
        self._last_action = "自动退房，进入全实例休息"
        self._phase = "cooldown"

    async def _finish_visit(self, reason: str) -> None:
        async with self._finish_lock:
            visit_id, area = self._visit_id, self._visit_area
            if not visit_id or not self.agent.is_auto_visit_current(visit_id):
                return
            if not self.agent.begin_auto_retirement(visit_id):
                return
            try:
                if reason != "disconnect":
                    self._phase = "waiting_reply"
                    try:
                        if not await self.agent.wait_for_reply_end(timeout=30):
                            await self.agent.stop_current_reply()
                        self._phase = "farewell"
                        await self._say("leave", visit_id)
                    except Exception as exc:
                        self._last_error = str(exc)
                self._phase = "leaving"
                for delay in (0, 2, 5):
                    if delay:
                        await self.clock.sleep(delay)
                    if not self.agent.is_auto_visit_current(visit_id):
                        return
                    try:
                        result = await self.agent.leave(source="auto", expected_visit_id=visit_id)
                        if not result.get("ok", True):
                            raise RuntimeError(result.get("error", "退房未成功"))
                        break
                    except Exception as exc:
                        self._last_error = str(exc)
                else:
                    await self.pause()
                    self._pause_reason = "退房失败，请检查当前语音状态后恢复"
                    return
                await self._cooldown_after_auto_exit(area)
            finally:
                if not self.agent.is_auto_visit_current(visit_id):
                    self._visit_id = ""
                    self._leave_at = self._empty_since = None
                self._wake.set()
