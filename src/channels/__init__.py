"""渠道能力工厂；导入不建立连接。"""

from collections.abc import Callable
from typing import Any

from ._internal.legacy import LegacyPlatformDirectory, LegacyRoomTransport
from ._internal.output.controls import VoiceOutputControls
from ._internal.output.duplex import VoiceDuplex
from ._internal.output.playback import RoomPlayback
from ._internal.output.vad import EnergyVad
from ._internal.session.service import RoomChannel
from .api import (
    AudioOutput,
    Channel,
    Duplex,
    OutputControls,
    OutputSettings,
    PlatformDirectory,
    Playback,
    RoomTransport,
    VoiceActivityDetector,
)
from .models import SessionRef, VadvConfig

__all__ = ["create_channel", "create_duplex", "create_oopz_channel", "create_oopz_directory", "create_output_controls", "create_playback", "create_vad"]


def create_playback(channel: Channel, session: SessionRef | None, output: Duplex) -> Playback:
    return RoomPlayback(channel, session, output)


def create_channel(transport: RoomTransport) -> Channel:
    return RoomChannel(transport)


def create_oopz_channel(bot: object, output: Callable[[], AudioOutput | None]) -> Channel:
    """旧语音路径装配入口；output 仅获取传输，不回调业务生命周期。"""
    return create_channel(LegacyRoomTransport(bot, output))


def create_oopz_directory(bot: object) -> PlatformDirectory:
    """为单个账号绑定构造只读平台查询，不建立连接。"""
    return LegacyPlatformDirectory(bot)


def create_duplex(transport: Any) -> Duplex:
    """注入浏览器传输，导入和构造均不连接平台。"""
    return VoiceDuplex(transport)


def create_output_controls(
    settings: OutputSettings, duplex: Callable[[], Duplex | None], joined: Callable[[], bool],
) -> OutputControls:
    return VoiceOutputControls(settings, duplex, joined)


def create_vad(config: VadvConfig | None = None) -> VoiceActivityDetector:
    return EnergyVad(config)
