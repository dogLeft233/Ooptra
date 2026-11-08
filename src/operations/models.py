"""系统维护已实现的配置类型；其他维护任务类型仍按设计逐步迁入。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutputVolume:
    """已确认可保存的输出音量百分比；临时静音不是持久化配置。"""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int or not 10 <= self.value <= 100:
            raise ValueError("voice.output_volume 需要 10 到 100 之间的整数")

    @classmethod
    def parse(cls, raw: object) -> OutputVolume:
        """保留配置编辑器对十进制整数字符串的兼容，布尔和小数均拒绝。"""
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("voice.output_volume 需要整数") from exc
        return cls(value)


@dataclass(frozen=True)
class VolumeSaveReceipt:
    """磁盘与运行配置字典已同步；不声称播放增益已经热应用。"""

    volume: int
