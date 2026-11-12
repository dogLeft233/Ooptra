"""连续发声抢话许可；没有PCM、播放或模型状态。"""
from collections import OrderedDict

from behavior.models import SpeechActivity


class ContinuousSpeech:
    def __init__(self) -> None:
        self._armed = True
        self._speech: OrderedDict[str, float] = OrderedDict()

    @property
    def armed(self) -> bool:
        return self._armed

    def rearm(self) -> None:
        self._armed = True
        self._speech.clear()

    def observe(self, activity: SpeechActivity, *, hold_ms: int) -> bool:
        if type(hold_ms) is not int or hold_ms < 0:
            raise ValueError("抢话门限须为非负整数毫秒")
        if not self._armed:
            return False
        if hold_ms == 0:
            self._armed = False
            self._speech.clear()
            return True
        if not activity.speaking:
            self._speech.pop(activity.user_key, None)
            return False
        previous = self._speech.pop(activity.user_key, 0.0)
        accumulated = previous + activity.duration_ms
        self._speech[activity.user_key] = accumulated
        # 成员来源有限；异常身份流量也不让策略状态无限增长。
        if len(self._speech) > 256:
            self._speech.popitem(last=False)
        if accumulated < hold_ms:
            return False
        self._armed = False
        self._speech.clear()
        return True
