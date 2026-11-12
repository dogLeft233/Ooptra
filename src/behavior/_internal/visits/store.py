"""Atomic, fail-closed quota and cooldown state for one voice connection.

Snapshots expose day, daily_count, area_counts, global_cooldown_until,
area_cooldowns, and pending (visit ID -> {area, reserved_at}). Dates use UTC+8;
timestamps use UTC ISO strings. Confirmed visit IDs persist separately for
idempotence across restarts and date changes. The day is a high-water mark:
clock rollback blocks joining rather than resetting quota repeatedly.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.config_file_store import config_file_write_lock, replace_text_files_atomically

_BEIJING = timezone(timedelta(hours=8))
_STATE_KEYS = {
    'version', 'day', 'daily_count', 'area_counts', 'global_cooldown_until',
    'area_cooldowns', 'pending', 'confirmed',
}


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('状态时间必须包含时区')
    return value.astimezone(timezone.utc)


def _day(now: datetime) -> str:
    return _utc(now).astimezone(_BEIJING).date().isoformat()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError('状态域或访问 ID 必须是非空字符串')
    return value


def _count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError('状态次数必须是非负整数')
    return value


def _timestamp(value: Any, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError('状态截止时间必须是 ISO 时间')
    return _utc(datetime.fromisoformat(value)).isoformat()


def _date(value: Any) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('状态日期必须是 ISO 自然日')
    return value


def _empty() -> dict[str, Any]:
    return {
        'version': 1, 'day': None, 'daily_count': 0, 'area_counts': {},
        'global_cooldown_until': None, 'area_cooldowns': {}, 'pending': {}, 'confirmed': {},
    }


def _validated(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _STATE_KEYS or type(raw['version']) is not int:
        raise ValueError('状态文件格式不完整或不支持')
    if raw['version'] != 1:
        raise ValueError('状态文件版本不支持')
    result = deepcopy(raw)
    if result['day'] is not None:
        _date(result['day'])
    _count(result['daily_count'])
    for field in ('area_counts', 'area_cooldowns', 'pending', 'confirmed'):
        if not isinstance(result[field], dict):
            raise ValueError(f'状态 {field} 必须是对象')
        for key in result[field]:
            _identifier(key)
    for count in result['area_counts'].values():
        _count(count)
    if sum(result['area_counts'].values()) != result['daily_count']:
        raise ValueError('状态全局与分域次数不一致')
    result['global_cooldown_until'] = _timestamp(result['global_cooldown_until'], nullable=True)
    result['area_cooldowns'] = {
        area: _timestamp(value) for area, value in result['area_cooldowns'].items()
    }
    for pending in result['pending'].values():
        if not isinstance(pending, dict) or set(pending) != {'area', 'reserved_at'}:
            raise ValueError('状态未确认记录格式错误')
        _identifier(pending['area'])
        pending['reserved_at'] = _timestamp(pending['reserved_at'])
    counts: dict[str, int] = {}
    for record in result['confirmed'].values():
        if not isinstance(record, dict) or set(record) != {'area', 'day'}:
            raise ValueError('状态已确认记录格式错误')
        _identifier(record['area'])
        _date(record['day'])
        if result['day'] is None or record['day'] > result['day']:
            raise ValueError('状态已确认记录日期错误')
        if record['day'] == result['day']:
            counts[record['area']] = counts.get(record['area'], 0) + 1
    if counts != result['area_counts']:
        raise ValueError('状态次数与已确认记录不一致')
    if set(result['pending']) & set(result['confirmed']):
        raise ValueError('状态访问记录不能同时未确认和已确认')
    if result['day'] is None and (result['pending'] or result['daily_count']):
        raise ValueError('状态访问记录缺少日期')
    return result


class AutoVisitStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._state = _empty()
        self._loaded = False
        self._known_file = False

    def load(self) -> None:
        """Missing files are new state; unreadable or malformed files raise."""
        with config_file_write_lock():
            # Leave an earlier in-memory state untouched when reading fails.
            self._loaded = False
            try:
                contents = self.path.read_text(encoding='utf-8')
            except FileNotFoundError as exc:
                if self._known_file:
                    raise OSError(f'自动串门状态文件已丢失: {self.path}') from exc
                state = _empty()
            except UnicodeError as exc:
                raise ValueError(f'自动串门状态文件编码损坏: {self.path}: {exc}') from exc
            except OSError as exc:
                raise OSError(f'读取自动串门状态失败: {self.path}: {exc}') from exc
            else:
                self._known_file = True
                try:
                    state = _validated(json.loads(contents))
                except (ValueError, TypeError, OverflowError) as exc:
                    raise ValueError(f'自动串门状态文件损坏: {self.path}: {exc}') from exc
            self._state = state
            self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _write(self, state: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            replace_text_files_atomically([
                (self.path, json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + '\n'),
            ])
        except OSError as exc:
            raise OSError(f'写入自动串门状态失败: {self.path}: {exc}') from exc
        self._state = state
        self._known_file = True

    def _rolled(self, now: datetime) -> dict[str, Any]:
        state = deepcopy(self._state)
        day = _day(now)
        if state['day'] is None or day > state['day']:
            state.update(day=day, daily_count=0, area_counts={})
        return state

    def snapshot(self, now: datetime) -> dict[str, Any]:
        with config_file_write_lock():
            self._ensure_loaded()
            state = self._rolled(now)
            if state != self._state:
                self._write(state)
            return {key: deepcopy(value) for key, value in state.items()
                    if key not in {'confirmed', 'version'}}

    def can_join(self, area: str, now: datetime, global_limit: int, area_limit: int,
                 *, check_cooldowns: bool = True) -> bool:
        _identifier(area)
        _count(global_limit)
        _count(area_limit)
        with config_file_write_lock():
            state = self.snapshot(now)
            if _day(now) < state['day'] or state['pending']:
                return False
            if check_cooldowns:
                for until in (state['global_cooldown_until'], state['area_cooldowns'].get(area)):
                    if until is not None and _utc(now) < datetime.fromisoformat(until):
                        return False
            return ((global_limit == 0 or state['daily_count'] < global_limit)
                    and (area_limit == 0 or state['area_counts'].get(area, 0) < area_limit))

    def reserve(self, visit_id: str, area: str, now: datetime) -> None:
        _identifier(visit_id)
        _identifier(area)
        with config_file_write_lock():
            self._ensure_loaded()
            state = self._rolled(now)
            if _day(now) < state['day']:
                raise ValueError('状态时钟回拨，不能预留自动串门')
            existing = state['confirmed'].get(visit_id) or state['pending'].get(visit_id)
            if existing:
                if existing['area'] != area:
                    raise ValueError('状态访问 ID 已用于其他域')
                return
            if state['pending']:
                raise ValueError('状态存在未确认的入房操作')
            state['pending'][visit_id] = {'area': area, 'reserved_at': _utc(now).isoformat()}
            self._write(state)

    def confirm(self, visit_id: str, area: str, now: datetime) -> None:
        _identifier(visit_id)
        _identifier(area)
        with config_file_write_lock():
            self._ensure_loaded()
            state = self._rolled(now)
            confirmed = state['confirmed'].get(visit_id)
            if confirmed:
                if confirmed['area'] != area:
                    raise ValueError('状态已确认访问域不匹配')
                if state != self._state:
                    self._write(state)
                return
            pending = state['pending'].get(visit_id)
            if pending is None or pending['area'] != area:
                raise ValueError('状态入房确认缺少匹配的预留记录')
            if _day(now) < state['day'] or _utc(now) < datetime.fromisoformat(pending['reserved_at']):
                raise ValueError('状态时钟回拨，不能确认自动串门')
            del state['pending'][visit_id]
            state['confirmed'][visit_id] = {'area': area, 'day': _day(now)}
            state['daily_count'] += 1
            state['area_counts'][area] = state['area_counts'].get(area, 0) + 1
            self._write(state)

    def rollback(self, visit_id: str) -> None:
        _identifier(visit_id)
        with config_file_write_lock():
            self._ensure_loaded()
            if visit_id in self._state['pending']:
                state = deepcopy(self._state)
                del state['pending'][visit_id]
                self._write(state)

    def set_global_cooldown(self, until: datetime) -> None:
        value = _utc(until).isoformat()
        with config_file_write_lock():
            self._ensure_loaded()
            state = deepcopy(self._state)
            state['global_cooldown_until'] = value
            self._write(state)

    def set_area_cooldown(self, area: str, until: datetime) -> None:
        _identifier(area)
        value = _utc(until).isoformat()
        with config_file_write_lock():
            self._ensure_loaded()
            state = deepcopy(self._state)
            state['area_cooldowns'][area] = value
            self._write(state)
