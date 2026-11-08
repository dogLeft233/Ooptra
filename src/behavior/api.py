"""行为决策、输入尝试、Live许可及串门任务的公开边界。"""

from datetime import datetime
from typing import Any, Literal, Protocol

from behavior.models import (
    AttemptFinished,
    AttemptPermit,
    AttemptRejected,
    AttemptsSnapshot,
    AudioReady,
    AutoVisitConfig,
    ControlAction,
    ControlProposal,
    DeferLive,
    FeedbackReceipt,
    LiveProposal,
    NoAction,
    NotExecuted,
    RecognitionObserved,
    ReplySettings,
    ScheduledReply,
    Speak,
    SpeechActivity,
)
from channels.models import (
    ActionOutcome,
    AreaMembership,
    JoinedAreas,
    PlaybackResult,
    RoomOperation,
    SessionRef,
)


class ReplyPolicy(Protocol):
    """概率、关键词规范化和有界成员窗口；调用者不维护窗口字典。

    observe 对已知成员延长关键词/连续对话窗口；未知成员不能获得窗口。
    clear 在换房/混合成员无法归属时清理；select 每次调用独立抽样，
    同一回合的最终选择及 Live 的迟到信息由 Behavior 决策台账持有。
    """

    def select(self, percent: int) -> bool: ...

    def matches_keywords(self, text: str, keywords: list[str]) -> bool: ...

    def observe(self, text: str, keywords: list[str], user_key: str, seconds: float) -> bool: ...

    def clear(self) -> None: ...


class MembershipDirectory(Protocol):
    """渠道只读目录端口；装配注入，不授予房间控制权。"""

    async def joined_areas(self) -> JoinedAreas: ...

    async def voice_members(self, area: str) -> AreaMembership: ...


class VisitsHost(Protocol):
    """串门需要的执行端口。实现须以 expected_* 拒绝旧观察的动作。

    status 返回兼容管理面的独立快照，调用者不能修改真实房间。
    directory 身份随账号绑定变化；operation_epoch 随人工控制递增。
    status 必含 room_state 与 room_cleanup_pending；只有 idle 且后者为 False
    才能确认失败进房已完整收尾，允许释放访问预留。缺失字段按未知处理。
    """

    @property
    def directory(self) -> MembershipDirectory: ...

    @property
    def operation_epoch(self) -> int: ...

    def status(self) -> dict[str, Any]: ...

    def is_auto_visit_current(self, visit_id: str) -> bool: ...

    def begin_auto_retirement(self, expected_visit_id: str) -> bool: ...

    async def join(self, area: str, channel: str, *, source: str = "manual",
                   visit_id: str = "", expected_operation_epoch: int | None = None) -> dict[str, Any]: ...

    async def leave(self, *, source: str = "manual", expected_visit_id: str | None = None,
                    expected_operation_epoch: int | None = None) -> dict[str, Any]: ...

    async def speak_announcement(self, kind: Literal["enter", "leave"], template: str, *,
                                 expected_visit_id: str, timeout: float = 20) -> dict[str, Any]: ...

    async def wait_for_reply_end(self, timeout: float = 30) -> bool: ...

    async def stop_current_reply(self) -> None: ...


class AutoVisits(Protocol):
    """拥有检查计划、冷却和访问配额；进退房执行由注入端口负责。

    start/stop 可重复调用；stop 等待循环及访问任务结束，不擅自退出人工房间。
    人工操作通知在渠道操作锁释放后交付。存储读取失败时暂停自动进入。
    status 为兼容管理面序列化快照；任务资源不从接口返回。
    """

    @property
    def config(self) -> AutoVisitConfig: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def reload(self, config: AutoVisitConfig) -> None: ...

    def status(self) -> dict[str, Any]: ...

    def set_bot_ready(self, ready: bool) -> None: ...

    def notify_presence(self, area: str, channel: str) -> None: ...

    async def on_operation(self, event: RoomOperation) -> None: ...


class VisitStore(Protocol):
    """原子文件保存访问预留/确认及冷却；返回快照与内部状态分离。"""

    def load(self) -> None: ...
    def snapshot(self, now: datetime) -> dict[str, Any]: ...
    def can_join(self, area: str, now: datetime, global_limit: int, area_limit: int,
                 *, check_cooldowns: bool = True) -> bool: ...
    def reserve(self, visit_id: str, area: str, now: datetime) -> None: ...
    def confirm(self, visit_id: str, area: str, now: datetime) -> None: ...
    def rollback(self, visit_id: str) -> None: ...
    def set_global_cooldown(self, until: datetime) -> None: ...
    def set_area_cooldown(self, area: str, until: datetime) -> None: ...


class ReplyScheduler(Protocol):
    """一个消费任务使用的最新输入信箱；reset 清理旧代次与冷却。

    新输入替换尚未消费的输入；冷却期间也只保留最新一条。
    超过冷却时长加三秒的输入不再回复。取消 next 由所属任务收尾。
    finish 幂等且拒绝旧代次，打断不产生冷却。
    """

    def offer(self, user_key: str, pcm: bytes, sample_rate: int) -> None: ...
    async def next(self, cooldown: float) -> ScheduledReply: ...
    async def prepare(self, item: ScheduledReply, cooldown: float) -> ScheduledReply | None: ...
    def finish(self, item: ScheduledReply, *, cooldown: float, interrupted: bool = False) -> None: ...
    def reset(self) -> None: ...


class BargeInGate(Protocol):
    """拥有每成员连续说话时长与每轮最多一次的抢话许可。

    仅在正在输出且策略允许抢话时提交音频活动；静音活动清理对应成员累计。
    observe 返回 True 即消费本轮许可，不等待模型/播放中断实际执行成功。
    hold_ms=0 保留第一帧立即抢话的兼容语义。rearm 清理全部累计和旧许可。
    """

    @property
    def armed(self) -> bool: ...
    def observe(self, activity: SpeechActivity, *, hold_ms: int) -> bool: ...
    def rearm(self) -> None: ...


class Behavior(Protocol):
    """按原会话和稳定触发登记尝试/行动；终态幂等并区分冲突。

    0额度不限；输入一经受理，沉默或失败不退还。Live有限等待不计级联额度。
    决策不执行SDK或模型。满载拒绝新触发，不能丢活跃关联以重新计费。
    """

    def start_session(self, session: SessionRef) -> None: ...
    def snapshot(self) -> AttemptsSnapshot: ...
    def exhausted(self, limit: int) -> bool: ...
    def admit(self, trigger: AudioReady, limit: int) -> AttemptPermit | AttemptRejected: ...
    def decide(self, observed: RecognitionObserved, settings: ReplySettings) -> Speak | NoAction | ControlAction: ...
    def decide_live(self, proposal: LiveProposal, settings: ReplySettings) -> Speak | NoAction | DeferLive: ...
    def decide_control(self, proposal: ControlProposal, settings: ReplySettings) -> ControlAction | NoAction: ...
    def finish_attempt(self, finished: AttemptFinished) -> FeedbackReceipt: ...
    def feedback(self, result: NotExecuted | ActionOutcome | PlaybackResult) -> FeedbackReceipt: ...
