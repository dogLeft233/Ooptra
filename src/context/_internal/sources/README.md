# 来源准备与 AstrBot RPC

状态：目标内部子模块，尚无 Python 实现。入口见 [上下文管理](../../README.md)。隐藏来源规则、人格准备、授权目标、外部请求及轮询租约。规则配置和长期记忆的权威仍在 AstrBot；这里不复制一份可独立修改的 group_map。

## 类型与内部入口

| 数据 | 字段与不变量 |
| --- | --- |
| `SourceBinding` | `binding_id, source_generation, room_scope, source_kind, persona_umo/persona_id, provider, memory_route_id, memory_targets`；目标保存完整 UMO、平台身份、群/私聊类别和 memory_persona_id，显式空目标不能回退默认 |
| `ContextRef` | `session_ref, source_generation, binding_id`；定位一次准备所用的不可变绑定，不能用当前设置重新解释旧请求 |
| `PreparedSource` | 绑定、人格、记忆摘要、来源/记忆新鲜度、明确降级状态；personality可用与RPC在线分开 |
| `RpcRequest` | `request_id, protocol=1, session_id, room, scope, created_at, operation, typed_payload`；只在本子模块协议适配中变成字典 |

- `prepare_source(request: SourceRequest) -> PreparedSource`：来源由已授权配置确定，模型不得指定目标；等待回执有界，返回仍绑定请求的会话/来源代次，调用方复查后安装。
- `recall(request: RecallRequest) -> RecallResult`：检查 ContextRef 与检索授权，查询所选目标；显式不可用不同于查无结果。
- `remember(request: RememberRequest) -> RememberReceipt`：验证写入授权和不可变绑定后，调用 memory 台账先持久化；不在本地建立第二份耐久 outbox。
- `bindings(request: BindingRequest) -> BindingResult`：具名分支为查询目录/保存来源/删除来源/保存映射/删除映射，保留现有协议；仅管理员入口调用，配置请求不受语音切房取消。
- `poll(request: PollRequest) -> PollResult`、`resolve(receipt: RpcReceipt) -> RpcResolution`：供已认证 HTTP 适配；receipt 校验 peer、request ID、操作范围，原始 JSON 必须先解析。PollResult具名区分已领取批次/等待截止/PeerConflict/Closed；RpcResolution区分Accepted/Duplicate/Unknown/Rejected，应用不直接修改 pending/outbox。

`ContextRef`、来源及工具请求/结果为 `context.models` 公共类型；RPC 传输类型是否公开只按 HTTP 入口实际需求决定，不能暴露 pending 字典。`BindingRequest` 只提供已有来源管理能力，不扩成通用远程命令。

## 授权与工具调用

`RecallRequest` 含 `tool_call_id, context_ref, query, speaker_ref`；`RememberRequest` 含 `tool_call_id, context_ref, content, speaker_ref, authorization`。内容沿用现有 1–2000 字边界；工具调用 ID 是幂等关联，不能充当授权。

`WriteAuthorization` 是context签发并登记的受限grant引用：`grant_id, context_ref, input_ref/admin_request_ref, approved_targets, expires_at`，批准理由与证据保存在context内部。`prepare`依据已经 `observe` 的原输入事实、确定的请求/管理员既有策略，返回可用 `authorized_write_grants`；没有证据就返回空集合，不要求app凭模型判断制造授权。模型只能提供content，app从本轮准备结果选取适用grant，remember按grant_id查登记并复核绑定/期限/目标，直接构造同形对象不能授权。

明确请求的识别用本项目已经批准的输入解析/授权规则；不能从生成模型自报“用户要求”推导写权限。语音转录无稳定说话者、多人混合或语义不明确且无管理员预授权时返回 `WriteDenied`。保留现有明确记忆工具能力可由管理员既有共享/写回授权策略授予，但该策略必须可定位和审查，不能隐含放宽。私聊共享/写回仍须已有管理员配置。旧上下文不能借工具请求写到新来源；若原绑定被撤销，新工具调用拒绝，已受理 outbox 保留原目标及审计状态，按既有协议处理，不自动重定向。

生成模块经 app 注入的 `ContextToolPort` 使用这些具名请求/结果，不能持有 `ContextBridge` 或访问 pending。模型 reader 不等待外部检索；独立工具任务有拥有者，取消只撤销未执行等待，不宣称第三方已经开始的写入被撤销。

## RPC 与状态机

准备状态：`Unprepared → Preparing → Ready/Degraded/Unavailable`。已有 Live 会话遇临时桥接离线保留已加载人格，但新检索明确失败；刷新/重进房提升来源代次。迟到 prepare/recall 不安装到新会话。

短 RPC pending 与 durable delivery 分开：pending 最多 16 个，轮询每批最多 8 个，最长等待 20 秒；保留现有操作租约（写/配置 25 秒，普通读取 8 秒）及 peer 35 秒在线判断，迁移不能悄悄改变。短读取超时结束等待；remember 超时返回原 DeliveryId 的 pending/unknown，不重新构造写入 ID。

sources 拥有网络等待 Future、peer 租约和可重建领取时间；memory 拥有 durable entry/逐目标回执。poll 合并短请求与 memory.ready；一次请求 ID 不在两处重复领取。resolve 先定位原请求，再由所属短 RPC 或 memory.apply_receipt 收尾。RPC 重复回执返回 duplicate/known，不重新写历史；未知 ID 明确 unknown。

关闭取消短读取与轮询 Future；bindings 是否继续由其管理任务拥有者决定，不绑语音房间生命周期。投递队列不随切房清空；坏恢复数据使诊断降级并停止不安全写入，不当作空队列。

## 依赖、迁移及验证

sources 可使用 memory 的窄投递端口；memory 不反向调用 sources。两者共享的 SourceBinding 定义在 context 公共类型。模块协调器读取 situation 并调用 prepare_source，不让来源层反向查询活动房间。

证据：`ContextBridge.prepare/request/poll/resolve/tool/clear` 与 [同步说明](../../../../docs/astrbot-context-sync.md)；当前 `clear` 保护 bindings、outbox保留原scope，迁移必须保留。先迁短 RPC/来源准备及公开工具端口，再把 durable delivery 接 memory，HTTP 保留现有路由和 protocol=1。

目标验证：来源切换迟到响应、显式空来源、私聊授权、同 peer 重连/其他 peer 租约拒绝、poll与remember不重复领取、超时未知、原 ID 重试、bindings不被切房取消。换 AstrBot 传输只影响本适配；更改记忆目标是领域契约变化，不能伪装成存储替换。当前均为设计分析。
