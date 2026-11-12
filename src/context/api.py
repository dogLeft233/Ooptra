"""上下文公开接口：版本化情境、来源授权和事务历史/可靠投递。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from channels.models import RoomTarget
from context.models import (
    BindingRequest,
    ConfirmedTurn,
    ContextRequest,
    ContextRevision,
    DeliveryStatus,
    LegacyExport,
    Observation,
    OutputRecord,
    PollRequest,
    PollResult,
    PreparedContext,
    RecallRequest,
    RecordReceipt,
    RememberRequest,
    RpcReceipt,
    RpcResolution,
    SituationSnapshot,
    VoiceTrace,
    VoiceTracePage,
    VoiceTraceQuery,
)


class ConversationHistory(Protocol):
    """同步 SQLite 历史视图；阻塞磁盘操作可由异步调用者交给线程。

    返回独立字典；生产输出应通过 ContextService.record_output 共同提交 outbox。
    """

    @property
    def path(self) -> str: ...
    def set_persona(self, persona: str) -> None: ...
    def get_persona(self) -> str: ...
    def append(self, role: str, content: str, *, user_key: str = "", channel_key: str = "") -> dict[str, Any]: ...
    def record(self, turn: ConfirmedTurn) -> RecordReceipt: ...
    def recent(self, *, user_key: str = "", limit: int | None = None) -> list[dict[str, Any]]: ...
    def as_messages(self, *, user_key: str = "", limit: int | None = None) -> list[dict[str, str]]: ...
    def clear(self, *, user_key: str = "") -> int: ...


class SourceBridge(Protocol):
    """AstrBot protocol=1 来源、短 RPC 与耐久待写适配。

    由应用所在事件循环串行调用。poll/resolve 仅供已认证 HTTP 适配；
    poll 返回独立事件，不能借返回对象重定向已排队目标。
    clear 使旧准备/读取失效，保留已受理写入与 bindings 管理请求。
    prepare 成功仅表示准备完成；record_turn 与 queued 不代表外部写入确认。
    """

    @property
    def active(self) -> bool: ...
    @property
    def connected(self) -> bool: ...
    @property
    def persona(self) -> str: ...
    @property
    def context(self) -> dict[str, Any]: ...
    @property
    def history(self) -> list[dict[str, str]]: ...
    @property
    def session_id(self) -> str: ...
    def status(self) -> dict[str, Any]: ...
    def clear(self) -> None: ...
    async def prepare(self, area: str, channel: str) -> None: ...
    async def request(self, operation: str, payload: dict) -> dict: ...
    async def tool(self, name: str, args: Any, *, speaker: str = "") -> dict: ...
    def record_turn(self, user_text: str, reply_text: str, *, speaker: str, interrupted: bool = False) -> None: ...
    async def poll(self, *, wait: float = 20.0, client_id: str = "") -> list[dict]: ...
    def resolve(self, request_id: str, result: dict) -> bool: ...
    async def poll_request(self, request: PollRequest) -> PollResult: ...
    def resolve_receipt(self, receipt: RpcReceipt) -> RpcResolution: ...
    async def bindings(self, request: BindingRequest) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class ContextService(Protocol):
    """权威台账与情境入口。

    运行期通过observe_async/snapshot_async及prepare_context处理磁盘等待；
    observe/snapshot/history是同步兼容入口，仍可能等待锁和磁盘，仅供离线调用，
    不与异步观察并发使用。record_output是有限同步事务，可由调用方安排线程。
    prepare_context 捕获不可变原来源，不隐式切换来源。
    同 action/input ID 异内容报 ValueError；关闭后不接收新提交，已提交记录可恢复。
    tools 返回本次准备绑定端口，写工具必须使用 context 签发的有效授权。
    """

    @property
    def history(self) -> ConversationHistory: ...
    @property
    def bridge(self) -> SourceBridge: ...

    def record_voice_trace(self, trace: VoiceTrace) -> None:
        """独立SQLite诊断表按完整会话/input更新；不进入模型历史或outbox。

        原输入身份不可变，阶段不倒退，最终文本不被临时文字覆盖。
        同步有限事务，关闭后拒绝；失败由调用边界安全记录，不伪造成功。
        """
        ...
    def enqueue_voice_trace(self, trace: VoiceTrace) -> bool:
        """所属事件循环非阻塞受理不可变诊断；True仅表示入队，False表示容量满。

        最多128项待写及一项在途，串行线程写入，失败由上下文安全记录。
        关闭后拒绝；close收完已受理项再关闭台账，调用者不管理线程或取消。
        """
        ...
    async def flush_voice_traces(self) -> None:
        """等候本次已受理诊断收尾；取消只撤回等待，不取消拥有的写入。"""
        ...
    def voice_traces(self, query: VoiceTraceQuery) -> VoiceTracePage:
        """倒序稳定游标分页，最多100条；刷新/重启保留，从功能启用后开始记录。"""
        ...
    def observe(self, observation: Observation) -> ContextRevision: ...
    def snapshot(self, scope: RoomTarget) -> SituationSnapshot: ...
    async def observe_async(self, observation: Observation) -> ContextRevision:
        """所属loop有界受理观察，等待真实落盘后安装投影并返回版本。

        至多128份在途（包括正在写入），满载抛BufferError，关闭后拒绝。
        按受理顺序串行提交；乱序输入仍落盘但不回退投影。磁盘等待在线程，
        可变情境仅在loop安装。调用者取消不撤销已受理工作；close排空并观察失败。
        未开始执行协程前取消不受理。已受理失败由上下文安全记录唯一堆栈，
        向等待者抛ObservationFailed；应用只记录自身流程失败终态，不重复堆栈。
        """
        ...
    async def snapshot_async(self, scope: RoomTarget) -> SituationSnapshot:
        """不阻塞loop地读取原范围；与异步观察串行，历史和台账版本同一事务。

        取消仍收完已开始的磁盘读取；close等待资源收尾。同步observe/snapshot及
        history兼容入口仍可等待磁盘/锁，仅供离线或由调用者安排阻塞执行。
        """
        ...
    async def prepare_context(self, request: ContextRequest) -> PreparedContext:
        """固定原房间/来源，准备近15分钟至多max_turns条多人历史并保留身份/时间。

        不按request.user_key过滤房间现场；精确排除本次input_ref，个人历史查询不变。
        当前input_ref的完整原输入才可签发记忆授权；指定说话者必须与原输入一致。
        不指定known_revision时一次取得当前准备结果及其版本；后续观察不改写该结果。
        指定known_revision用于严格版本约束；先查快照不能预留版本或保证随后准备成功。
        已知版本冲突/关闭失败；台账读取不阻塞loop，与异步观察串行；
        历史和台账版本来自同一事务。首次await（包括内部锁等待）前固定原来源；
        等待期间换绑不改投新来源，也不为原输入向新来源签授权。不等待外部来源网络。
        """
        ...
    def record_output(self, record: OutputRecord) -> RecordReceipt: ...
    def delivery(self, delivery_id: str) -> DeliveryStatus: ...
    async def recall(self, request: RecallRequest) -> dict[str, Any]: ...
    async def remember(self, request: RememberRequest) -> dict[str, Any]: ...
    def tools(self, prepared: PreparedContext) -> ContextTools: ...
    async def export_legacy(self, directory: Path) -> LegacyExport: ...
    async def close(self) -> None: ...


class ContextTools(Protocol):
    """只绑定一个准备结果的模型工具端口，不暴露底层 SourceBridge。"""

    @property
    def active(self) -> bool: ...
    @property
    def persona(self) -> str: ...
    @property
    def context(self) -> dict[str, Any]: ...
    @property
    def history(self) -> list[dict[str, str]]: ...
    @property
    def session_id(self) -> str: ...
    async def recall(self, request: RecallRequest) -> dict[str, Any]: ...
    async def remember(self, request: RememberRequest) -> dict[str, Any]: ...
    async def tool(self, name: str, args: Any, *, speaker: str = "") -> dict[str, Any]: ...
