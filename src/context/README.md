# 上下文管理

当前状态：**部分迁移**。本地 JSONL 历史、具名确认回合提交与 AstrBot 来源/RPC/outbox 已进入本模块；情境投影、完整事务台账与授权 grant 仍是目标契约。已实现接口以 [api.py](api.py)、[models.py](models.py) 和包工厂为准。全局规则见 [系统架构](../../ARCHITECTURE.md)，目录约束见 [代码要求](../../docs/coding-standards.md#模块目录与接口导航)。

## 职责与非职责

提供“根据已发生的事实准备相关情境，并可靠记录实际结果”的能力。隐藏人格来源选择、历史裁剪、存储和 AstrBot 同步。拥有本地历史、观察版本及持久化投递状态；外部长记忆仍由 AstrBot 拥有。房间快照只是渠道状态的带版本投影；不决定接话，也不持有模型或播放句柄。

## 子模块导航与依赖

| 子模块 | 独有知识和状态 | 详细设计 |
| --- | --- | --- |
| 情境投影 | 渠道事实排序/去重、成员和输入观察、版本及新鲜度 | [situation](./_internal/situation/README.md) |
| 历史与可靠投递台账 | 历史、幂等键、outbox和逐目标回执的共同提交及恢复 | [memory](./_internal/memory/README.md) |
| 来源准备与 AstrBot RPC | 来源规则/授权、人格准备、工具请求、短RPC和传输租约 | [sources](./_internal/sources/README.md) |

目标内部依赖：模块协调器 → situation、memory、sources；sources → memory的窄投递端口；memory和situation不反向导入sources。共享数据用本模块具名类型。协调器只组合快照与准备/记录流程，不保存第二份来源、历史或当前房间状态。

比较后选择三项深能力：单一大ContextBridge容易混淆临时等待和耐久状态；把历史与outbox拆开则迫使调用者处理双写与恢复。因此将二者合并为台账，把独立变化的情境投影和外部协议分开。内部目录只保存本次必要设计，Python文件随真实迁移创建。

## 已实现使用入口

应用通过 `context.create_memory(path, max_turns)` 和 `context.create_context_bridge(timeout=..., outbox_path=...)` 装配；旧 `voice_agent.memory`、`voice_agent.context_bridge` 已移除。

- `ConversationHistory` 拥有本地 JSONL 文件与人格。`record(ConfirmedTurn)` 在同一锁、一次文件写入中提交输入及已确认助手回复，返回本地 `RecordReceipt`；未完成输出不得携带完整助手文字。返回成功不代表 fsync 或 AstrBot 写入确认，也不保证跨提交幂等。`append` 保留独立输入记录与兼容能力，实际输出由应用确认后提交。
- `SourceBridge` 拥有来源准备、短 RPC、待写队列与租约。`prepare` 结果绑定原准备代次；`clear` 不清理已受理写入和 `bindings`。`context`、`history`、`poll` 都返回独立快照，不开放可变队列。
- protocol=1 写请求先写 outbox 再提供给 worker；取消等待保留原 request ID/原目标。落盘失败抛出 `OSError`，不宣称已排队；恢复数据不合法时拒绝启动写入。构造器的 `outbox_path=None` 仅用于明确选择内存模式的调用方。

同步历史和 outbox 文件操作仍会阻塞调用线程；异步调用方应按实际路径将耗时操作交给线程。当前 JSONL 历史与 outbox JSON 仍分别保存，**未实现共同事务**；近期历史尚未按房间/来源严格隔离。下面设计中的 `record(OutputRecord)`、`delivery`、授权 grant 与 SQLite 导入不能视作当前 `ConversationHistory.record(ConfirmedTurn)` 已具备的能力。

## 待实现公开契约

- `observe(observation: Observation) -> ContextRevision`：接收已解析渠道事件或识别结果；按来源 ID/序号拒绝重复或迟到覆盖。允许保留原始发生事实，但不让旧会话事实覆盖新会话投影。识别输入通过memory按input_ref提交独立用户历史，后续沉默/失败也保留已观察输入；渠道高频状态观察只更新投影，不逐帧写历史。
- `snapshot(scope: Scope) -> SituationSnapshot`：只读、按用途裁剪的环境与历史，带版本、来源新鲜度及渠道会话观察；未同步与真实空值分别表示。行为决策不读取存储内部对象。
- `prepare(request: ContextRequest) -> PreparedContext`：按指定 scope、已知版本、输入/意图准备人格、对话及记忆；返回使用的实际版本、来源绑定及按已观察输入/管理员策略签发的 `authorized_write_grants`。来源不可用时明确返回可用的本地降级或 `ContextUnavailable`，不得默默切换外部记忆写入目标。
- `record(record: OutputRecord) -> RecordReceipt`：`OutputRecord` 包含渠道 `ActionOutcome` 和对应可确认表达，不把模型生成完成当成成功输出；按动作 ID 去重。返回本地已记录及可选 `DeliveryId`，**不代表外部确认**。
- `delivery(id: DeliveryId) -> DeliveryStatus`：区分待发、重试中、已确认、永久失败和未知。HTTP 轮询/回执适配只向本模块交付解析后的请求，不操作队列字典。
- `recall(request: RecallRequest) -> RecallResult`、`remember(request: RememberRequest) -> RememberReceipt`：保留现有模型记忆工具；具名请求绑定本次 `ContextRef` 和工具调用ID，写入还需验证 `WriteAuthorization`。模型不能自行选择目标或批准写入；字段与失败语义见 sources 设计。
- `bindings(request: BindingRequest) -> BindingResult`：保留现有管理员来源/映射管理，独立于语音会话取消；不新建本地权威 group_map。来源由此管理请求配置，prepare只按已授权规则解析。
- `poll(request: PollRequest) -> PollResult`、`resolve(receipt: RpcReceipt) -> RpcResolution`：只给已认证的AstrBot HTTP适配使用；前者区分已领取/等待截止/PeerConflict/Closed，后者区分Accepted/Duplicate/Unknown/Rejected；不开放 pending/outbox可变容器。

`SituationSnapshot`、`PreparedContext`、`ContextRef`、`Observation`、`OutputRecord`、工具请求/结果与 `RecordReceipt` 归 `context.models`；可引用 `channels.models` 中范围和执行事实。情境版本不要求决策、检索和执行形成跨模块事务；执行安全由渠道复查，策略过时由行为重新决策。

## 生命周期与故障

准备期间切房：结果仍绑定原范围，运行时丢弃过时的本轮准备；不能把旧人格装入新 Live 会话。记录和投递保存事件创建时的来源，即使用户后续切人格也不改写队列目标。关闭停止新请求，持久化已受理记录并回收轮询等待；持久化失败必须返回失败，不能先确认后丢失。生成失败仍可保留已观察输入；部分播放仅标记已知部分，未知内容不补成完整助手回复。

## 依赖与验证

只允许向下引用 `channels.models` 和公共技术机制；不导入行为、生成、维护或 WebUI。AstrBot 协议在自己的适配内部；[现有同步说明](../../docs/astrbot-context-sync.md) 是迁移兼容依据，不改写外部协议。

验证入口：`tests/context/` 覆盖来源快照隔离、准备完成后切换、取消写入后的原 ID 恢复、磁盘失败与坏恢复数据，以及 JSONL 解析和具名回合提交；既有 `tests/test_astrbot_context_bridge.py`、`tests/test_astrbot_live_context.py`、`tests/test_memory_recent.py` 保留协议与运行回归。执行 `python -m pytest -q tests/context tests/test_astrbot_context_bridge.py tests/test_astrbot_live_context.py tests/test_memory_recent.py`。尚未验证目标事务台账、按 action ID 幂等与完整授权 grant。

## 最小数据形状与调用

`ContextRevision` 含 `session_ref, observation_version, source_generation, history_revision`，各分量只由对应拥有者推进；`PreparedContext` 含该版本、`ContextRef`、人格、裁剪后的对话/相关记忆及来源/降级说明。`ContextRequest` 含 scope、原输入引用、所需版本和准备用途，不引用behavior意图类型，以免反向依赖。`OutputRecord`含原范围/会话、action ID、来源绑定、实际渠道结果及可确认表达；授权grant由context根据已observe的真实输入/已有管理策略签发，模型不能用普通字段自造。无输出也可记录房间动作事实，不虚构助手文本。

仅凭公开契约：观察输入 → snapshot → prepare(原范围/版本) → 返回绑定结果；执行结束 record → 获本地回执/可选DeliveryId → delivery查询外部状态。外部离线/来源过时、存储失败、投递满载是不同失败，不能一律返回空上下文。生成工具通过注入端口调用recall/remember，不访问内部sources。

## 实现与阅读顺序

依次阅读 `api.py` → `models.py` → 所需 `_internal/` 实现。内部能力是职责划分，不要求立即创建同名目录、类或文档；具有独立状态和维护需求时再增加子模块说明。跨模块只使用公开契约，工厂由应用装配，禁止返回内部可变对象。迁移批次与验收见 [重构准备计划](../../docs/superpowers/plans/2026-11-08-module-refactor-readiness.md)。
