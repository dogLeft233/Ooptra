"""Validated auto-visit settings and sparse, non-mutating configuration patches.

Only override fields accept ``None`` in patches (remove the override). An area
row of ``None`` deletes that area; explicit empty prompt lists remain silent.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from behavior.models import AutoVisitConfig

_DEFAULTS = {
    'join_probability': 0.2,
    'stay_minutes': (10.0, 20.0),
    'auto_cooldown_minutes': (30.0, 60.0),
    'manual_cooldown_minutes': (120.0, 240.0),
    'enter_prompts': ['在玩什么游戏？'],
    'leave_prompts': ['拜拜，我下了'],
}
_TOP_KEYS = {'check_interval_minutes', 'daily_limit', 'defaults', 'areas'}
_AREA_KEYS = {'enabled', 'overrides', 'daily_limit'}



def _mapping(value: Any, name: str, allowed: set[str] | None = None) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f'{name} 必须是对象')
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f'{name} 字段名必须是字符串')
    if allowed is not None and set(value) - allowed:
        raise ValueError(f'{name} 包含未知字段: {sorted(set(value) - allowed)}')
    return dict(value)


def _detached(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _detached(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_detached(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_detached(item) for item in value)
    return deepcopy(value)


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} 必须是有限数字')
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'{name} 必须是有限数字') from exc
    if not math.isfinite(result):
        raise ValueError(f'{name} 必须是有限数字')
    return result


def _range(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(f'{name} 必须包含两个分钟值')
    minimum, maximum = (_number(item, name) for item in value)
    if not 1 <= minimum <= maximum <= 10080:
        raise ValueError(f'{name} 必须在 1~10080 分钟内且最小值不大于最大值')
    return minimum, maximum


def _limit(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'{name} 必须是非负整数')
    return value


def _fields(raw: Any, name: str) -> dict[str, Any]:
    fields = _mapping(raw, name, set(_DEFAULTS))
    result = {}
    for key, value in fields.items():
        label = f'{name}.{key}'
        if key == 'join_probability':
            probability = _number(value, label)
            if not 0 <= probability <= 1:
                raise ValueError(f'{label} 必须在 0~1 范围内')
            result[key] = probability
        elif key.endswith('_minutes'):
            result[key] = _range(value, label)
        else:
            if not isinstance(value, list) or len(value) > 50:
                raise ValueError(f'{label} 必须是最多 50 条的台词列表')
            prompts = []
            for item in value:
                if not isinstance(item, str) or len(item.strip()) > 500:
                    raise ValueError(f'{label} 每条台词必须是最多 500 字的字符串')
                if item.strip():
                    prompts.append(item.strip())
            result[key] = prompts
    return result


def _area_id(area: Any) -> str:
    if not isinstance(area, str) or not area.strip() or area != area.strip():
        raise ValueError('域 ID 必须是非空且无首尾空白的字符串')
    return area


def parse_auto_visit_config(raw: Mapping[str, Any] | None) -> AutoVisitConfig:
    fields = _mapping({} if raw is None else raw, 'auto_visit', _TOP_KEYS)
    defaults = deepcopy(_DEFAULTS)
    defaults.update(_fields(fields.get('defaults', {}), 'defaults'))
    areas = {}
    for area, item in _mapping(fields.get('areas', {}), 'areas').items():
        _area_id(area)
        entry = _mapping(item, f'areas.{area}', _AREA_KEYS)
        enabled = entry.get('enabled', False)
        if not isinstance(enabled, bool):
            raise ValueError(f'areas.{area}.enabled 必须是布尔值')
        areas[area] = {
            'enabled': enabled,
            'daily_limit': _limit(entry.get('daily_limit', 0), f'areas.{area}.daily_limit'),
            'overrides': _fields(entry.get('overrides', {}), f'areas.{area}.overrides'),
        }
    return AutoVisitConfig(
        _range(fields.get('check_interval_minutes', [10, 20]), 'check_interval_minutes'),
        _limit(fields.get('daily_limit', 3), 'daily_limit'), defaults, areas,
    )


def effective_area(config: AutoVisitConfig, area: str) -> dict[str, Any]:
    """Return an independent validated snapshot; absent areas are disabled."""
    _area_id(area)
    entry = config.areas.get(area, {})
    result = _detached(config.defaults)
    result.update(_detached(entry.get('overrides', {})))
    result.update(enabled=entry.get('enabled', False), daily_limit=entry.get('daily_limit', 0))
    return result


def merge_auto_visit_patch(
    raw: Mapping[str, Any], patch: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge supplied fields only and reject the whole patch if invalid."""
    parse_auto_visit_config(raw)
    result = _detached(_mapping(raw, 'auto_visit', _TOP_KEYS))
    changes = _mapping(patch, 'patch', _TOP_KEYS)
    for key, value in changes.items():
        if key == 'defaults':
            result.setdefault(key, {}).update(_detached(_mapping(value, key, set(_DEFAULTS))))
        elif key == 'areas':
            areas = result.setdefault('areas', {})
            for area, change in _mapping(value, 'areas').items():
                _area_id(area)
                if change is None:
                    areas.pop(area, None)
                    continue
                entry = areas.setdefault(area, {})
                for field, updated in _mapping(change, f'areas.{area}', _AREA_KEYS).items():
                    if field == 'overrides':
                        overrides = entry.setdefault(field, {})
                        for name, override in _mapping(updated, field, set(_DEFAULTS)).items():
                            if override is None:
                                overrides.pop(name, None)
                            else:
                                overrides[name] = _detached(override)
                    else:
                        entry[field] = _detached(updated)
        else:
            result[key] = _detached(value)
    parse_auto_visit_config(result)
    return result


def saved_prompts(config: AutoVisitConfig, kind: str, area: str = "") -> list[str]:
    """查询公告候选的独立快照；空列表保留明确静默语义。"""
    if kind not in {"enter", "leave"}:
        return []
    policy = effective_area(config, area) if area else config.defaults
    return list(policy[kind + "_prompts"])
