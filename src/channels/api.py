"""渠道公开接口；execute 等待执行结束，snapshot 只返回观察。"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from .models import (
    ActionOutcome,
    ActionState,
    AreaMembership,
    AudioPushResult,
    ChannelCommand,
    ChannelSnapshot,
    JoinedAreas,
    RoomTarget,
    SessionRef,
)

RemotePcmHandler = Callable[[str, bytes, int], Awaitable[None] | None]


class TransportFailure(RuntimeError):
    """适配器已识别的外部失败；正文不含供应商原始载荷。"""


class AudioOutput(Protocol):
    """迁移期浏览器音频边界；返回载荷由适配器解析。"""

    async def push_tts_pcm(
        self, pcm16: bytes, sample_rate: int, *, finish: bool = False
    ) -> object: ...
    async def stop_tts(self) -> object: ...


class RoomTransport(Protocol):
    """外部房间传输边界，不包含模型或业务策略。"""

    async def join(self, target: RoomTarget) -> str: ...
    async def leave(self) -> None: ...
    async def retire_output(self) -> None: ...
    async def push_audio(
        self, pcm: bytes, rate: int, *, finish: bool = False
    ) -> AudioPushResult: ...


class Channel(Protocol):
    async def execute(self, command: ChannelCommand) -> ActionOutcome:
        """等待进退房事实及必要收尾。

        同 ID/命令等待原任务；异载荷拒绝。仅首个调用者的取消撤销执行，
        清理后传播 CancelledError；重复等待者的取消不撤销原动作。
        失败效果可能不确定，调用者依据 snapshot.state 决定后续恢复。
        本切片只支持 JoinRoom/LeaveRoom，尚无完整播放动作与事件流。
        """
        ...

    async def push_audio(
        self, session: SessionRef, pcm: bytes, rate: int, *, finish: bool = False
    ) -> AudioPushResult:
        """迁移期最小播放入口；绑定原会话，不能替代完整播放终态。"""
        ...

    def snapshot(self) -> ChannelSnapshot: ...
    def outcome(self, action_id: str) -> ActionState:
        """区分运行、终态和未知；本实例去重，终态至少保留十分钟。

        活动上限 128、总量 1024；容量不足拒绝新动作，允许查询和重复等待。
        不提供跨进程/跨重启幂等保证。
        """
        ...


class PlatformDirectory(Protocol):
    """绑定单个账号的只读目录；失败/畸形响应不是空房间事实。

    查询使用十秒超时，SDK 需配合取消；返回不可变数据。快照不保证执行时仍有效，
    自动进房前须重新查询。它不拥有或更改当前实际房间。
    """

    async def joined_areas(self) -> JoinedAreas: ...
    async def voice_members(self, area: str) -> AreaMembership: ...


class OutputSettings(Protocol):
    """输出控制只需要可更新的音量值，不读取整份应用配置。"""

    output_volume: int


class Duplex(Protocol):
    """有界音频采集和播放适配；创建者负责 start/close。

    采集队列最多 200 帧，满时丢弃新帧。close 注销捕获并等待消费任务结束，
    迟到跨线程帧按所属事件循环拒绝。wait_tts_complete 才确认播放结束。
    迁移期 transport 仅供 app 安装旧浏览器回调；不得用于更改实际房间。
    """

    output_volume: int
    output_muted: bool

    @property
    def transport(self) -> Any: ...

    def set_remote_pcm_handler(self, handler: RemotePcmHandler | None) -> None: ...
    def enqueue_remote_pcm(self, uid: str, pcm: bytes, sample_rate: int) -> None: ...
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def enable_listen(self, enabled: bool = True) -> dict[str, Any]: ...
    async def push_tts_pcm(self, pcm16: bytes, sample_rate: int, *, finish: bool = False) -> dict[str, Any]: ...
    async def set_output_volume(self, volume: int) -> dict[str, Any]: ...
    async def stop_tts(self) -> dict[str, Any]: ...
    async def wait_tts_complete(self, timeout: float = 30) -> dict[str, Any]: ...


class OutputControls(Protocol):
    """唯一拥有临时静音状态；持久化保存通过注入异步端口进行。

    保存成功与实际音量应用分别报告，取消写入等待已提交文件工作收尾。
    configuration_update 与 set 串行；reset_mute 使在途旧会话应用失效。
    """

    muted: bool

    def configuration_update(self) -> AbstractAsyncContextManager[None]: ...
    def set_saver(self, saver: Callable[[int], Awaitable[None]]) -> None: ...
    def attach(self, duplex: Duplex) -> None: ...
    def reset_mute(self) -> None: ...
    async def sync_volume(self, *, unmute: bool = False) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...
    async def set(self, *, volume: int | None = None, muted: bool | None = None) -> dict[str, Any]: ...
    async def adjust(self, target: Callable[[int], tuple[int | None, bool | None]]) -> dict[str, Any]: ...


class VoiceActivityDetector(Protocol):
    """按能量切句，返回完整有声 PCM；窗口只由本实例拥有，换会话时 reset。"""

    def feed(self, pcm16: bytes) -> bytes | None: ...
    def reset(self) -> None: ...
