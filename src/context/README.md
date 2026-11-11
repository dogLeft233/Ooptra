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

1. 流式识别最终成功后，无论信箱是否选中、bot是否正在生成/播放，都提交输入观察；未完成/失败的识别不成为完整输入。识别结果乱序完成仍保存一次，不回退当前投影。渠道事实或识别结果转换成 `Observation` 后 `observe`。完整识别按 `input_ref` 先持久化用户输入，再安装投影；沉默、生成失败仍保留输入。Live 增量文本 `complete=False` 仅更新投影，不提交半句话、不签发写授权。
2. `snapshot(RoomTarget)` 返回不可变情境；`prepare_context(ContextRequest)` 捕获原范围、版本、来源、人设和裁剪历史。已知版本过期报冲突，不锁住渠道跨网络等待。来源准备由 `.bridge.prepare` 在进房/刷新时完成，每次生成不重新解析来源。
准备历史按 `ContextRequest.input_ref` 的精确持久标识排除本次输入，避免供应商再追加本次问题时重复；相同文字的旧输入照常保留。历史浏览和情境快照仍包含已持久化的本次输入。Live 原输入已通过实时音频/转录进入供应商，传入 input_ref 的准备也遵循此排除规则，不额外把本次转录当历史重放。

3. 生成只读取 `PreparedContext` 和 `tools(prepared)` 绑定端口。`RecallRequest`/`RememberRequest` 绑定 `ContextRef`；写入还验证登记的 `WriteAuthorization`、期限和完整原输入。模型参数不决定目标。
4. 渠道完成后 `record_output(OutputRecord)` 接收真实 `ActionOutcome` 或 `PlaybackResult`。只有 completed 可携完整助手文字。历史、幂等记录和可选外部投递在同一 SQLite 事务提交；`RecordReceipt` 区分本地成功、已排队与容量拒绝，外部是否确认由 `delivery` 查询。
5. HTTP 适配使用具名 `PollRequest`、`RpcReceipt.parse`、`BindingRequest.parse`，通过 `.bridge.poll_request/resolve_receipt/bindings` 调用。协议 v1 的 JSON 外形、批量/租约限制保持兼容。

同动作同内容重试返回原回执，异内容报冲突；已观察输入由 `input_ref` 去重。满载保留本地历史和原投递载荷，后来用相同记录重试仅补受理，不重复历史。部分目标确认持久化，重启后只向原来源的未确认目标重投，request ID 不变。切来源不会改写已受理投递。

## 范围与授权

生成用近期历史按真实房间和来源隔离，取近15分钟内至多 `max_turns` 条（默认30条），按确认入库顺序保留所有说话者的 `user_key/ts`，不按当前说话者过滤；`HistoryMessage` 的 assistant 角色表示机器人发言，其 `user_key` 仅为原接话对象。个人历史 `history.recent(user_key=...)` 仍独立筛选，不因房间聚合扩大范围。

同一房间的用户观察可供新来源读取，其他来源的助手回复不混入。旧 JSONL 缺乏可信范围，只保留在管理员/兼容历史视图，不能猜房间后注入生成。来源离线返回显式 `local` 降级，使用本地人格，不悄悄把写入改到新目标。

写授权仅在完整、稳定说话者的原输入包含明确记忆请求时签发，默认有效 120 秒；准备请求指定的说话者必须与该原输入一致，不能借旁人的输入申请授权；不以模型自报的“用户要求”授权。来源刷新立即使新工具调用失效；已提交的投递保留原目标。实时转录尚未完成时没有写授权，工具应如实报告不可写。显式记忆入队返回 queued，外部回执前不返回已长期保存。

## 生命周期与验证

同步 `observe/record_output` 是有限本地事务，异步装配可按顺序交给线程；不得并发无序安装观察。`close` 拒绝新请求、结束短 RPC/轮询，等待台账锁释放；不等待第三方在线。原始文件、数据库迁移与降级出口见 [memory](./_internal/memory/README.md)。

公开行为测试：`python -m pytest -q tests/context tests/test_astrbot_context_bridge.py tests/test_astrbot_live_context.py tests/test_memory_recent.py`。覆盖真实 SQLite 回滚、重启、同键冲突、满载补受理、原来源、逐目标重试、增量输入、授权伪造/撤销和旧文件导出。原协议测试继续验证切房、取消、peer 与持久化故障。模块只允许引用 `channels.models` 和公共技术机制。

控制台运行期诊断使用 `enqueue_voice_trace(VoiceTrace)`，返回值仅表示受理：最多128条待写和1条在写，队满返回False并记录安全告警。上下文独占串行写入任务，磁盘事务在线程中执行；`flush_voice_traces()` 等待已受理写入，`close()` 停止受理并排空，等待者取消不能丢弃写入或提前关库。写入失败记录原会话/输入ID，不影响发言；同步 `record_voice_trace(VoiceTrace)` 仅供离线调用。`voice_traces(VoiceTraceQuery)` 查询当前已落盘记录，SQLite独立表保存从识别到实际结果的输入投影，按完整会话及input_ref幂等更新、阶段单调前进，未知说话者只允许补全一次，已知身份不能更换。游标倒序分页每页最多100条，重启保留；不进入history/outbox，不增加情境版本。验证：`tests/context/test_voice_traces.py`。

多人房间验证：`tests/context/test_room_conversation.py` 覆盖跨成员、跨房间/来源、近期容量、重启、乱序与个人授权；`tests/test_aliyun_deepseek.py` 验证忙碌期间识别、接话判断和正式模型请求。历史不会回填诊断表中旧的未选中输入。
