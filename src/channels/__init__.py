"""渠道能力工厂；导入不建立连接。"""

from collections.abc import Callable

from ._internal.legacy import LegacyPlatformDirectory, LegacyRoomTransport
from ._internal.session.service import RoomChannel
from .api import AudioOutput, Channel, PlatformDirectory, RoomTransport

__all__ = ["create_channel", "create_oopz_channel", "create_oopz_directory"]


def create_channel(transport: RoomTransport) -> Channel:
    return RoomChannel(transport)


def create_oopz_channel(bot: object, output: Callable[[], AudioOutput | None]) -> Channel:
    """旧语音路径装配入口；output 仅获取传输，不回调业务生命周期。"""
    return create_channel(LegacyRoomTransport(bot, output))


def create_oopz_directory(bot: object) -> PlatformDirectory:
    """为单个账号绑定构造只读平台查询，不建立连接。"""
    return LegacyPlatformDirectory(bot)
