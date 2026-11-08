"""行为能力工厂；导入不会启动后台任务。"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from behavior._internal.replies.policy import MemberReplyPolicy
from behavior._internal.replies.scheduler import LatestReplies
from behavior._internal.visits.service import AutoVisitController
from behavior._internal.visits.settings import (
    effective_area,
    merge_auto_visit_patch,
    parse_auto_visit_config,
    saved_prompts,
)
from behavior._internal.visits.store import AutoVisitStateStore
from behavior.api import (
    AutoVisits,
    BargeInGate,
    Behavior,
    ReplyPolicy,
    ReplyScheduler,
    VisitsHost,
    VisitStore,
)
from behavior.models import AutoVisitConfig


def create_reply_policy(
    *, clock: Callable[[], float] = time.monotonic,
    random_value: Callable[[], float] | None = None,
) -> ReplyPolicy:
    return MemberReplyPolicy(clock=clock, random_value=random_value or random.random)


def create_visit_store(path: str | Path) -> VisitStore:
    """创建持久化访问记录；不修改现有文件。"""
    return AutoVisitStateStore(path)


def create_auto_visits(
    host: VisitsHost, config: AutoVisitConfig, store: VisitStore | None = None, *,
    path: str | Path | None = None, enabled: Callable[[], bool],
    clock: Any = None, rng: Any = None,
) -> AutoVisits:
    """装配串门调度；仅 start 启动循环，enabled 每次读取当前开关。"""
    if store is None:
        if path is None:
            raise ValueError("必须提供访问存储或路径")
        store = create_visit_store(path)
    return AutoVisitController(host, config, store, enabled=enabled, clock=clock, rng=rng)

__all__ = ["create_auto_visits", "create_barge_in_gate", "create_reply_policy", "create_reply_scheduler", "create_visit_store", "effective_area", "merge_auto_visit_patch", "parse_auto_visit_config", "saved_prompts"]


def create_reply_scheduler(
    *, clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ReplyScheduler:
    return LatestReplies(clock=clock, sleep=sleep)


def create_barge_in_gate() -> BargeInGate:
    """装配逐成员连续发声门限；不启动任务。"""
    from behavior._internal.replies.barge_in import ContinuousSpeech

    return ContinuousSpeech()


def create_behavior(*, clock: Callable[[], float] = time.monotonic,
                    random_value: Callable[[], float] | None = None) -> Behavior:
    from behavior._internal.replies.decisions import BehaviorService
    return BehaviorService(clock=clock, random_value=random_value or random.random)


def departure_template(config: AutoVisitConfig | None = None, area: str = "", *, rng=None) -> str:
    """手动语音离场沿用该域模板；随机选择和缺省台词归行为策略。"""
    config = config or parse_auto_visit_config({})
    candidates = saved_prompts(config, "leave", area) or ["拜拜，我下了"]
    return (rng or random).choice(candidates)
