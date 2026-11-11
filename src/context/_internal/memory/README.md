# 历史与可靠投递台账

状态：目标内部子模块，尚无 Python 实现。入口见 [上下文管理](../../README.md)。把历史记录、幂等键和 outbox 收敛在一个提交边界，隐藏存储及恢复；来源选择和 HTTP 租约不在这里。

## 类型与提交边界

| 数据 | 必需字段及含义 |
| --- | --- |
| `HistoryEntry` | `entry_id, scope, session_ref, input_ref/action_id, role, content, occurred_at, confirmation`；confirmation 区分输入事实、确认输出、部分/未知，不写完整生成草稿冒充已输出 |
| `SourceBinding` 引用 | 记录创建时的不可变 `binding_id` 和完整目标快照；来源定义见 [来源与 RPC](../sources/README.md)，不保存指向当前可变设置的引用 |
| `DeliveryEntry` | `delivery_id, request_id, operation=commit/remember, original_session, source_binding, payload, created_at, state, per_target_receipts`；request ID 重试不变 |
| `RecordCommit` | 本地历史变更、可选投递事件、幂等键及内容摘要；同键不同内容返回冲突，不能静默覆盖 |

- `commit_input(observation: RecognizedInput) -> InputRecordReceipt`：上下文observe调用，按input_ref幂等提交已观察用户事实；即使后续NoAction/NoSpeech无action也不依赖助手回复才能保存，空识别只保存状态不造用户文字。后续record引用同input_ref，不再重复用户历史。
- `commit_record(record: OutputRecord) -> RecordReceipt`：同一持久化事务写历史、幂等键及可选 outbox；事务提交成功才返回“本地已记录”。外部离线不阻止合法 outbox 入队。
- `enqueue_remember(request: RememberRequest, binding: SourceBinding) -> RememberReceipt`：先持久化再向租约领取开放；返回待确认，不声称已存长期记忆。
- `recent(query: HistoryQuery) -> HistorySlice`：范围、来源/会话和可选说话者显式限定，读取结果不可变；不能沿用旧 `recent(user_key)` 就声称已经按房间隔离。
- `ready(limit: int) -> tuple[DeliveryEntry,...]`、`apply_receipt(receipt: TargetReceipt) -> DeliveryStatus`：sources 子模块经窄端口调用；仅持久化回执，不操作 HTTP Future。
- `delivery(id: DeliveryId) -> DeliveryStatus`：未知 ID、待发、部分确认、重试、确认、永久失败分别表示。

`record` 对同动作多次到达只提交一次；相同动作后续更完整的可信确认需独立 `confirmation_revision`，只允许单调补充已确认范围，不重发已经确认的目标。首批实现不支持补充确认时，应明确拒绝这种更新，不用覆盖伪装幂等。

## 存储决定与恢复

比较：继续独立 JSONL + outbox JSON，改动少，但崩溃时两文件不能共同提交；为其补恢复日志会重复实现事务。目标首个实现使用标准库 SQLite 的事务能力，建立独立的上下文数据库，**不使用或改动 OneBot 身份映射数据库**。数据库路径由已解析的现有记忆文件路径派生为同目录 `ooptra_context.sqlite3`；迁移前校验与旧文件/映射库不冲突、目录可写、数据库schema归属正确，不能打开任意已有SQLite当新库。本设计不启用新配置或新文件，路径约定落地时同步操作指南。

最小逻辑表为 `history`、`deliveries`、`delivery_targets`、`idempotency`、`migration_state`；唯一键对应上述 ID。具体索引由范围查询与恢复测试决定，不预建通用 repository 框架。数据库连接/事务只在本子模块适配中使用；同步事务在线程中执行，单写入拥有者串行提交，不在事件循环阻塞，也不跨第三方网络等待持事务。

旧 `voice_memory.jsonl`、`astrbot_voice_outbox.json` 只通过可回退导入读取：先备份/校验、以稳定导入键提交、核对条目数/原 request ID/原来源、标记完成，再切换拥有者。格式不合法则停止并报告，不推测缺失来源、丢掉待写或自动清空。切换前旧拥有者停止写入；不能双写两套权威历史。旧文件保留以支持人工恢复，新数据的回退/导出需要在该批次明确，不能直接丢弃新记录。数据库版本不支持时拒绝启动写入，不能当空库。

## 投递状态机与界限

`Pending → Leased → Confirmed`；可重试/部分失败回到 Pending，仅包含未确认目标；明确永久错误转 Failed。租约是可重建的传输状态，进程崩溃后过期重领；**超时未知不等于写入失败**，保持原 ID/原目标等待回执。全部目标确认才整体成功；协议 captured/stored 含义沿用 [外部同步协议](../../../../docs/astrbot-context-sync.md)。

保留现有 128 条 outbox 容量约束，记忆写请求占用原 ID 的预留槽位；满载返回明确 `OutboxFull`，不得先记录“已排队”。本地输出历史可以成功而外部投递因满载未受理，但 `RecordReceipt` 必须明确 `local_recorded + delivery_rejected`；两者不是伪造一个事务整体成功。

投递满载后必须可补受理：首次事务在历史幂等记录内保留原绑定、待投递载荷和 `delivery_admission=RejectedCapacity`。调用者用相同键/相同内容重试 `record` 时，不重复历史，只尝试将这份尚未受理投递在新事务内加入outbox并更新同一回执；成功后该记录的DeliveryId稳定，后续重试只返回已有状态，已确认目标不重发。重试仍用原来源，不按新设置定向；原授权已撤销则返回明确Rejected，不默默换目标。满载记录不伪装已入队；这些信息随历史耐久保留，修复入口是公开record，无需额外通用retry框架。

持久化失败：未提交不对外确认，原内存状态保持；实际输出已发生时由调用方保留同幂等键重试记录，不重播语音。关闭停止新提交、等待正在提交的有限事务；已受理条目重启可恢复，等待第三方不是停机前提。不能承诺跨第三方严格 exactly-once。

## 证据、切片与验证

当前 `MemoryStore.append/recent` 和 `ContextBridge.record_turn/_persist/resolve` 分开维护历史与投递；`_persist` 捕获 OSError 后仅记错误，尚不满足目标失败回执。现有测试入口：`tests/test_memory_recent.py`、`tests/test_astrbot_context_bridge.py`。先迁移 record/delivery 和恢复，再接新 prepare；明确迁移旧后端写入方后再删除旧接口。

目标公开行为测试：提交故障不成功、重试同键不重复、同键异载荷冲突、部分播放不写全文、切来源后原队列目标不变、逐目标重试、坏旧文件不静默丢失、重启恢复。变化场景：替换事务存储只改本子模块及导入适配，不改行为/生成；这是设计分析，当前未验证新数据库实现。
