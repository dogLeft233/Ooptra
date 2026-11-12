# 上下文管理

本模块拥有版本化情境、已确认历史、来源授权及可靠投递；公开接口见 [api.py](api.py)、数据见 [models.py](models.py)。全局职责见 [系统架构](../../ARCHITECTURE.md)。外部长记忆归 AstrBot；本模块不控制房间、不调用生成供应商、不判断是否接话。

## 导航与状态归属

| 内部能力 | 隐藏的知识 | 导航 |
| --- | --- | --- |
| 情境投影 | 观察排序、重复/迟到、成员完整性、情境版本 | [situation](./_internal/situation/README.md) |
| 事务台账 | 历史、输入/动作幂等、原始 outbox、逐目标回执、导入导出 | [memory](./_internal/memory/README.md) |
| 来源与 RPC | 人格来源、授权、短请求、peer 与领取租约 | [sources](./_internal/sources/README.md) |

`create_context_service(memory_path, ...)` 装配同一个 SQLite 台账和来源适配；`.history` 是本地历史窄视图，`.bridge` 是认证 HTTP 使用的协议适配。生产输出通过 `record_output` 提交，不能分别调用历史 `record` 与来源 `record_turn` 双写。`create_memory` 是同一 SQLite 实现的独立历史工厂；`create_context_bridge` 是同一 SQLite 台账上的独立协议工厂；未指定 outbox 路径时使用内存 SQLite，不保留旧 JSON 写入分支。生产装配不再使用两份独立持久化拥有者。

## 真实调用路径

1. 渠道事实或识别结果转换成 `Observation` 后 `observe`。完整识别按 `input_ref` 先持久化用户输入，再安装投影；沉默、生成失败仍保留输入。Live 增量文本 `complete=False` 仅更新投影，不提交半句话、不签发写授权。
2. `snapshot(RoomTarget)` 返回不可变情境；`prepare_context(ContextRequest)` 捕获原范围、版本、来源、人设和裁剪历史。已知版本过期报冲突，不锁住渠道跨网络等待。来源准备由 `.bridge.prepare` 在进房/刷新时完成，每次生成不重新解析来源。
准备历史按 `ContextRequest.input_ref` 的精确持久标识排除本次输入，避免供应商再追加本次问题时重复；相同文字的旧输入照常保留。历史浏览和情境快照仍包含已持久化的本次输入。Live 原输入已通过实时音频/转录进入供应商，传入 input_ref 的准备也遵循此排除规则，不额外把本次转录当历史重放。

3. 生成只读取 `PreparedContext` 和 `tools(prepared)` 绑定端口。`RecallRequest`/`RememberRequest` 绑定 `ContextRef`；写入还验证登记的 `WriteAuthorization`、期限和完整原输入。模型参数不决定目标。
4. 渠道完成后 `record_output(OutputRecord)` 接收真实 `ActionOutcome` 或 `PlaybackResult`。只有 completed 可携完整助手文字。历史、幂等记录和可选外部投递在同一 SQLite 事务提交；`RecordReceipt` 区分本地成功、已排队与容量拒绝，外部是否确认由 `delivery` 查询。
5. HTTP 适配使用具名 `PollRequest`、`RpcReceipt.parse`、`BindingRequest.parse`，通过 `.bridge.poll_request/resolve_receipt/bindings` 调用。协议 v1 的 JSON 外形、批量/租约限制保持兼容。

同动作同内容重试返回原回执，异内容报冲突；已观察输入由 `input_ref` 去重。满载保留本地历史和原投递载荷，后来用相同记录重试仅补受理，不重复历史。部分目标确认持久化，重启后只向原来源的未确认目标重投，request ID 不变。切来源不会改写已受理投递。

## 范围与授权

准备历史按真实房间和来源隔离；同一房间的用户观察可供新来源读取，其他来源的助手回复不混入。旧 JSONL 缺乏可信范围，只保留在管理员/兼容历史视图，不能猜房间后注入生成。来源离线返回显式 `local` 降级，使用本地人格，不悄悄把写入改到新目标。

写授权仅在完整、稳定说话者的原输入包含明确记忆请求时签发，默认有效 120 秒；不以模型自报的“用户要求”授权。来源刷新立即使新工具调用失效；已提交的投递保留原目标。实时转录尚未完成时没有写授权，工具应如实报告不可写。显式记忆入队返回 queued，外部回执前不返回已长期保存。

## 生命周期与验证

语音装配使用 `create_voice_context`，由上下文模块拥有旧 outbox 文件命名和导入约定；`storage_configuration_changed` 判定何时需要重建存储。应用只在停止相关流程后关闭并重建，不拼装数据库或 outbox 路径。

同步 `observe/record_output` 是有限本地事务，异步装配可按顺序交给线程；不得并发无序安装观察。`close` 拒绝新请求、结束短 RPC/轮询，等待台账锁释放；不等待第三方在线。原始文件、数据库迁移与降级出口见 [memory](./_internal/memory/README.md)。

公开行为测试：`python -m pytest -q tests/context tests/test_astrbot_context_bridge.py tests/test_astrbot_live_context.py tests/test_memory_recent.py`。覆盖真实 SQLite 回滚、重启、同键冲突、满载补受理、原来源、逐目标重试、增量输入、授权伪造/撤销和旧文件导出。原协议测试继续验证切房、取消、peer 与持久化故障。模块只允许引用 `channels.models` 和公共技术机制。

完整输入的观察事务耐久捕获原来源和稳定 input_ref。无输出终态调用 `finish_input` 采集原输入；正常表达通过 `record_output` 一次合并原输入和确认输出。两者共用输入投递身份，已领取/确认载荷不改写；已采集终态后出现迟到输出只记录独立本地事实，不再次投递问题。容量拒绝保留原载荷，重试不跟随新来源。

同 input_ref 的归属从已知变为未知时，observe 撤销该输入已有写授权，清除已记录输入及未发布来源载荷中的 speaker。旧 PreparedContext/工具端口仍需通过当前授权及原输入身份校验，不能借旧成员写记忆；不改写已经对外受理的历史载荷。

Live 关闭交付的完整观察可携原 `ContextRef`，来源已刷新时仍按原绑定保存用户事实，不替换新来源的当前投影，也不为新来源签发该输入的写授权。
