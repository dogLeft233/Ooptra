"""渠道能力工厂；导入不建立连接。"""

from collections.abc import Callable
from typing import Any

from ._internal.adapters.voice import OopzDirectory, OopzRoomTransport
from ._internal.output.controls import VoiceOutputControls
from ._internal.output.duplex import VoiceDuplex
from ._internal.output.vad import EnergyVad
from ._internal.session.service import RoomChannel
from .api import (
    AudioIngress,
    AudioOutput,
    Channel,
    Duplex,
    NameDirectory,
    OutputControls,
    OutputSettings,
    PlatformConfiguration,
    PlatformDirectory,
    PlatformGateway,
    PlatformLogin,
    PlatformState,
    RemotePcmHandler,
    RoomTransport,
    VoiceActivityDetector,
    VoiceBinding,
    VoiceLifecycle,
)
from .models import AudioInputPolicy, VadvConfig

__all__ = [
    "create_audio_ingress",
    "create_channel",
    "create_duplex",
    "create_oopz_channel",
    "create_oopz_directory",
    "create_output_controls",
    "create_platform_gateway",
    "create_platform_login",
    "create_platform_state",
    "create_vad",
    "create_voice_binding",
    "name_directory",
]


def create_channel(transport: RoomTransport) -> Channel:
    return RoomChannel(transport)


def create_oopz_channel(bot: object, output: Callable[[], AudioOutput | None], *, controls=None) -> Channel:
    """平台房间端口装配；output仅提供音频能力，不回调业务生命周期。"""
    return create_channel(OopzRoomTransport(bot, output, controls))


def create_oopz_directory(bot: object) -> PlatformDirectory:
    """为单个账号绑定构造只读平台查询，不建立连接。"""
    return OopzDirectory(bot)


def create_duplex(transport: Any) -> Duplex:
    """注入浏览器传输，导入和构造均不连接平台。"""
    return VoiceDuplex(transport)


def create_output_controls(
    settings: OutputSettings, duplex: Callable[[], Duplex | None], joined: Callable[[], bool],
) -> OutputControls:
    return VoiceOutputControls(settings, duplex, joined)


def create_vad(config: VadvConfig | None = None) -> VoiceActivityDetector:
    return EnergyVad(config)


def create_voice_binding(bot: object, *, on_audio: RemotePcmHandler,
                         controls: Callable[[], OutputControls], on_gap: Callable[[str], None] | None = None) -> VoiceBinding:
    from ._internal.session.binding import VoiceBinding as PlatformVoiceBinding
    return PlatformVoiceBinding(bot, on_audio, controls, on_gap)


def create_platform_state() -> PlatformState:
    from ._internal.adapters.bridge.state import BridgeState
    return BridgeState()


def create_platform_gateway(state: PlatformState | None = None, *, configuration: PlatformConfiguration | None = None,
                            voice: VoiceLifecycle | None = None, retry_interval: float = 30.0, sdk_factory=None) -> PlatformGateway:
    from ._internal.adapters.bridge.app import BridgeController
    from ._internal.adapters.bridge.state import BridgeState
    if state is not None and not isinstance(state, BridgeState):
        raise ValueError("平台状态须由create_platform_state创建")
    return BridgeController(state if state is not None else BridgeState(),
                            configuration=configuration, voice=voice, retry_interval=retry_interval, sdk_factory=sdk_factory)


def name_directory() -> NameDirectory:
    """只读名称目录及显式异步补全；不返回SDK网关。"""
    from ._internal.adapters.oopz.name_resolver import NameDirectoryView
    return NameDirectoryView()


def create_platform_login(controller: PlatformGateway, *, configuration: PlatformConfiguration) -> PlatformLogin:
    from ._internal.adapters.oopz.login import OopzLoginService
    return OopzLoginService(controller, configuration)



def create_audio_ingress(policy: AudioInputPolicy, *,
                         resolve_speaker: Callable[[str], str] = lambda value: value) -> AudioIngress:
    from ._internal.output.input import AudioIngress as InputAssembler
    return InputAssembler(policy, resolve_speaker)
