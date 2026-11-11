"""集中保存成员窗口与接话选择算法，不依赖生成供应商配置。"""

import random
import time
import unicodedata
from collections.abc import Callable


class MemberReplyPolicy:
    """Bounded member-specific follow-ups; unknown speakers never gain a window."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 random_value: Callable[[], float] = random.random) -> None:
        self._clock = clock
        self._random = random_value
        self._deadlines: dict[str, float] = {}

    def clear(self) -> None:
        self._deadlines.clear()

    def observe(self, text: str, keywords: list[str], user_key: str, seconds: float) -> bool:
        named = self.matches_keywords(text, keywords)
        if seconds <= 0:
            self.clear()
            return named
        seconds = max(1, min(300, seconds))
        now = self._clock()
        self._deadlines = {key: value for key, value in self._deadlines.items() if value > now}
        active = bool(user_key and self._deadlines.get(user_key, 0) > now)
        if text.strip() and user_key and (named or active):
            if user_key not in self._deadlines and len(self._deadlines) >= 128:
                self._deadlines.pop(next(iter(self._deadlines)))
            self._deadlines[user_key] = now + seconds
        return named or active

    def select(self, percent: int) -> bool:
        if percent <= 0:
            return False
        if percent >= 100:
            return True
        return self._random() < percent / 100

    def matches_keywords(self, text: str, keywords: list[str]) -> bool:
        def normalize(value: str) -> str:
            folded = unicodedata.normalize("NFKC", value).casefold()
            return "".join(char for char in folded if char.isalnum())

        normalized_text = normalize(text)
        return any(keyword and keyword in normalized_text for keyword in map(normalize, keywords))
