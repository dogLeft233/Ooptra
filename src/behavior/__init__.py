"""行为能力工厂；导入不会启动后台任务。"""

import random
import time
from collections.abc import Callable

from behavior._internal.replies.attempts import SessionAttempts
from behavior._internal.replies.policy import MemberReplyPolicy
from behavior.api import CascadeAttempts, ReplyPolicy


def create_cascade_attempts() -> CascadeAttempts:
    return SessionAttempts()


def create_reply_policy(
    *, clock: Callable[[], float] = time.monotonic,
    random_value: Callable[[], float] | None = None,
) -> ReplyPolicy:
    return MemberReplyPolicy(clock=clock, random_value=random_value or random.random)
