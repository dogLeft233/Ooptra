"""系统维护公开能力：当前仅实现音量配置持久化切片。"""

from __future__ import annotations

from typing import Protocol

from operations.models import OutputVolume, VolumeSaveReceipt


class Configuration(Protocol):
    async def save_output_volume(self, volume: OutputVolume) -> VolumeSaveReceipt:
        """同进程串行修补磁盘并同步该运行字段，返回不表示播放已应用。

        模块内部在线程完成文件 I/O；调用者直接等待，不管理线程。文件保留注释、其他值、
        权限及符号链接；不承诺跨进程隔离、请求幂等或多文件崩溃事务。
        校验/写入失败抛出异常，文件写入成功前不变更运行字典。
        已受理保存会在取消后继续收尾，确认真实完成后再传播取消；重复取消不遗留工作。
        保存失败优先抛出原异常，取消不掩盖失败。调用者仍负责保存后的业务热应用。
        """
        ...
