"""渠道公开接口；execute 等待执行结束，snapshot 只返回观察。"""

from typing import Protocol

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
