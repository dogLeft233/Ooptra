"""系统维护公开能力：当前仅实现音量配置持久化切片。"""

from __future__ import annotations

from typing import Protocol

from operations.models import OutputVolume, VolumeSaveReceipt


class Configuration(Protocol):
    def save_output_volume(self, volume: OutputVolume) -> VolumeSaveReceipt:
        """同进程串行修补磁盘并同步该运行字段，返回不表示播放已应用。

        此方法含阻塞文件 I/O，异步调用者应在线程执行。文件保留注释、其他值、
        权限及符号链接；不承诺跨进程隔离、请求幂等或多文件崩溃事务。
        校验/写入失败抛出异常，文件写入成功前不变更运行字典。
        线程启动后，异步拥有者必须观察其完成，不得在取消时假定写入停止。
        """
        ...
