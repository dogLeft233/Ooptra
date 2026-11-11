# 实时模型会话

状态：**Gemini 会话实现已迁入，提议/许可式接口待实现**。当前 [会话实现](gemini_live.py) 拥有 WebSocket、重连、迟到转写和工具任务；使用注入策略，结束或丢弃回合通过不可变 `GeneratedTurn` 回调交给应用，完全不写权威历史。以下完整事件/许可契约仍是目标设计，本文不是已验证供应商保证。公开能力见 [对话生成](../../README.md)，实际房间/播放归渠道，接话策略归行为，来源与记忆归上下文。

## 边界与选择

保留独立 Live 会话而非复用逐句 `generate`：网络连接、服务端 VAD、迟到转录和模型轮次是持续状态；每句重连会改变现有行为。也不把 reader、策略和播放器作为一个大对象：会话只处理供应商协议、轮次关联和许可闸门，策略判断通过公开事件交给行为。

内部候选文件为 `_internal/live/session.py`（任务与连接拥有者）、`turns.py`（本地轮次与输出闸门）、`models.py`；Gemini 编解码为 `_internal/adapters/gemini.py`。`turns` 不知道 SDK、政策参数或记忆存储，`session` 不实现概率/冷却规则。初期这些职责可落在少量文件，不为每个状态建类或转发层。

## 内部接口与任务所有权

| 内部接口 | 输入/结果 | 拥有的责任 |
| --- | --- | --- |
| `start(LiveSessionRequest) -> LiveSession` | 固定渠道 session、PreparedContext、有限预算 | 启动连接、setup、reader/writer/dispatcher；就绪后返回，失败关闭半建资源 |
| `LiveSession.send_input(LiveInput) -> InputReceipt` | 音频/明确人工台词分支；带 input_ref、speaker、顺序 | 校验绑定和背压；收包不是回应许可，不写历史 |
| `LiveSession.events() -> AsyncIterator[LiveEvent]` | 唯一控制事件消费者 | 识别观察、提议/修订、获批输出引用、ControlProposed/ControlCancelled、轮次终态、连接状态 |
| `LiveSession.resolve_turn(ProposalRef, TurnPermission) -> ResolveReceipt` | 提议引用含 live_session_id、local_turn_id、revision；许可有显式变体 | 拒绝旧版本/过期许可，绑定一次 action，确保一轮只输出一次 |
| `LiveSession.resolve_control(ToolRef, ControlResolution) -> ControlResolveReceipt` | 由行为授权产生的渠道实际结果或明确拒绝/未执行 | 仅回传对应工具请求，不执行命令；回传前复查旧连接/工具取消 |
| `LiveSession.snapshot() -> LiveSnapshot` | 不阻塞的只读连接/当前轮次状态和最近故障 | 事件投递失败后仍可检查失败原因；不靠消费已满队列才能收尾 |
| `LiveSession.aclose() -> None` | 幂等 | 封闭 generation，撤销未终结轮次、工具任务、重连、reader/writer，等待资源关闭 |

reader 只解析网络事件，放入有界数据结构后继续读；dispatcher 发提议/结果，输出泵在获批后释放音频。writer 串行发送输入与工具响应。reader 不等待行为许可、播放完成或 AstrBot HTTP 回执。工具任务单独受控，关闭时由 session 回收；不得让 reader 取消并 await 自己。

## 数据与不变量

| 类型/状态 | 必要字段或含义 |
| --- | --- |
| `LiveBinding` | `live_session_id`、渠道 `session_ref`、`context_ref`、`connection_generation`；重连增加 generation，旧回调失效 |
| `LocalTurn` | `local_turn_id`、`proposal_revision`、关联 input_refs（可空/多条）、说话者已知/混合/未知、转录状态、输出序号、provider_state（收集中/完成/中断）、许可状态、有限截止 |
| `TurnProposed/TurnUpdated` | 提议引用、渠道会话、可用转录/输入引用、是否仍可能收到转录、提议截止；不包含策略判断结果 |
| `TurnPermission` | `Allow(SpeechIntent)`、`Hold(until)` 或 `Reject(reason)`；不接受裸 bool。Allow 绑定 action_id、有效期及同一会话 |
| `ToolRef` / `ControlProposed` | live_session_id、connection_generation、provider_tool_call_id、稳定 trigger_id、可证明的 input_ref/未知、已解析 ControlCandidate；同一工具请求不因事件重发生成新触发 |
| `ControlResolution` | Executed(ActionOutcome)/Rejected(reason)/NotExecuted(stage,reason)/Unknown(action_id)；保留独立 action 与工具引用关联，未知不是执行失败 |
| `TurnFinished` | 本地轮次、关联 action（未批准可无）、正常/拒绝/超时/中断/失败；不是渠道播放结果 |

当前协议没有可靠的全局轮次 ID。适配器按观察到的模型输出开始和 `turnComplete/interrupted` 分配本地 ID；转录独立到达时只关联可证明的范围，混合说话者保持未知。`connection_generation` 变化后，旧消息、许可和输出不得关联到新连接轮次。

resolve_turn 返回 Applied、Duplicate(original_binding) 或明确 Stale/Expired/Conflict；Allow 未受理且能确认没有绑定该 action 时，应用反馈 NotExecuted(stage=permission)，释放行为 pending。已获批的重复请求返回原 action 绑定，即使收到旧 revision 也不能把原动作误终结；新 action 试图占用已绑定轮次则冲突并只终结未受理的新 action。响应未知先读 snapshot 的当前/最后轮绑定；无法确认时保留未知并撤销该会话流程，不能伪报未执行后重发。

转录出现新信息时同一提议递增 revision，再交行为复评；不能创建新 action 或重复预留额度。行为处理相同触发修订必须幂等。Allow 后拒绝普通迟到修订，除非明确取消当前 action；Reject/超时/已关闭轮次不能复活。无法归属的迟到输入仍可作为独立观察给上下文，不把它接到新轮助手输出。若拒绝/超时/容量耗尽后供应商无法安全划分后续新旧输出，关闭旧模型连接并从准备上下文重建；不能直接给迟到音频分配新轮 ID。

控制工具往返：reader 解析退房/音量请求并发 ControlProposed → 应用把稳定触发及候选交 behavior → 具名渠道命令执行或明确拒绝 → 应用用 resolve_control 回传结果。候选不能绕过授权，模型所报用户身份/参数必须经解析；无法证明输入来源时保留未知，由行为决定是否允许。reader 不等待行为或渠道，独立 dispatcher/工具任务保留有限待处理表。控制结果只转换为模型工具响应，不在生成里执行 SDK 命令或使用 ContextToolPort。

同 ToolRef 重复上报只关联原 action/结果，不重新执行。工具取消时发 ControlCancelled；尚未执行者由应用反馈 NotExecuted，已进入渠道者调用渠道取消/查询并使用实际结果，不能声称外部执行已撤销。权限拒绝回传 Rejected，不构造成功承诺。响应发送前复查 connection_generation；退房可能已关闭模型连接，旧结果仍在渠道台账成立但不得发到新 socket，也不能为获得工具 ACK 再次退房。结果/发送 ACK 未知时沿原 action 查询，禁止换 ID 重试副作用。

## 状态机与许可

会话：`opening → ready → closing → closed`；临时连接故障可 `ready → reconnecting → ready/failed`，重连受次数与总期限限制，关闭后不可重连。上下文来源或渠道会话变化需建立新会话对象，不热改绑定。

轮次：

```text
collecting → proposed/pending → allowed → completed
                            ↘ rejected
                            ↘ expired
allowed → interrupted / failed
任意非终态 → cancelled（会话关闭）
```

`Hold` 只延长到该提议自己的有限截止，用于转录尚不完整时等新信息；它不是“先说再检查”，不释放输出、不预留动作额度。新的转录到达再发修订，或到截止时发最后一次提议，仍未获批则过期。模型虽已完成计算，未获批轮次仍可保留有限等待窗口；生成完成不决定其播放资格。`turnComplete` 先更新 provider_state，不提前发出普通 TurnFinished 让应用丢弃未决缓存；后续获批则一次释放缓存并正常结束输出流，拒绝/超时则发对应无输出终态。输出生成终态仍不代表渠道 drain 成功。

这保留当前“音频先到、关键词转录后到”的兼容需求：现有 `_MAX_DEFERRED_AUDIO_BYTES` 为 4 MiB，`_LATE_TRANSCRIPTION_SECONDS` 为 2 秒。首批适配保留这两个边界作为待验证预算，行为不把概率未选中立即误当永久拒绝；是否等待关键词属于行为的明确 DeferLive 决策。Hold 不允许重置计时并永久保留一轮。等待旧轮时若已观察到可靠的新输出轮边界，旧轮标为 superseded 后清缓存，迟到转录不得复活旧轮或给新轮授权；边界无法确认时按关闭重建处理。强制台词也携带唯一 action，不能和模型自动轮次并行创建第二个播放器。

## 背压与故障收尾

- 供应商音频大块先按完整 PCM 采样切为每帧至多渠道 64 KiB，再按已绑定 action/output 连续编号；4 MiB 是累计预算，不是单帧长度。未获批音频尚无 action 时内部保存带本地轮次的原片段，获批后统一切分/编号，不能提前伪造 action。未获批音频累计超过 4 MiB：清空该轮缓存，形成容量失败，不播不完整前缀。获批后的音频通路也有有限字节预算；慢播放不能堵住协议 reader。出队困难超过本轮期限时中止轮次并通知渠道取消，不丢中间音频后仍报告完整成功。
- 控制/终态事件不能按音频策略静默丢弃。初始控制队列设计上限 128 项；消费者失联耗尽容量时先把 session 状态设为 failed 并保留 event_delivery_overflow 原因，再取消未终结轮次/关闭连接；这些状态经 `snapshot()` 可读，不等待往已满队列写终态。snapshot 只承诺本对象连接状态、当前/最后一轮摘要，不承诺无限历史查询或跨重启恢复。字节/条数/时间预算是内部实现参数，需在适配测试证明后落地，不新增通用队列框架。
- 服务端中断立即使本轮后续输出失效并发 `TurnFinished(interrupted)`；应用取消对应渠道 action，渠道再清排期。生成关闭不等于已停止扬声器；渠道清理独立确认。
- 远端关闭/setup 超时必须清掉 ready 与连接引用，不把已死 socket 报为 active。重连后不自动重播已输出轮次；缓存从 PreparedContext 重建，不从模型缓存恢复权威历史。
- 待执行记忆工具最多沿用当前 4 个独立任务的有限并发；过载返回类型化 Busy。工具取消不能撤销外部已开始写入，结果未知/待确认交上下文投递查询，不向模型承诺保存成功。响应发送前复查当前 connection_generation，旧结果不写入新 socket。
- `aclose` 停止接收和重连、使所有提议/输出失效、取消并等待工具与输入/输出任务、关闭 socket，观测异常后清理。锁内不 await 依赖同锁的任务；资源清理有有限总期限，超时对外报告失败，不吞异常假装关闭完成。

## 隔离演练与迁移门槛

只读公开契约的调用：创建固定 S1/ContextRef 的 LiveSession → 持续 send_input → 消费 TurnProposed → 行为给 Hold 或 Allow/Reject → resolve_turn → 获批输出交渠道 → 执行结果交上下文/行为；S1 切换后 aclose，迟到许可返回 StaleProposal。应用无需知道 Gemini JSON、锁或延迟缓存。

设计故障序列：先出音频 → Hold → turnComplete → 2 秒窗口内关键词转录修订 → Allow → 仅一次释放原缓存；超时后的相同转录不复活；Allow 与切房并发则渠道仍拒绝旧 S1；工具未回执时关闭，旧结果不发送新连接；控制工具提出退房/音量先经行为与渠道，工具取消后不得把新轮误退房，退房成功但原 socket 关闭不重发动作；Allow 重复响应返回原绑定、未受理才发 NotExecuted(permission)；慢消费者不能阻止收到 interrupted。

现有证据为 `GeminiLiveBackend._handle_server_msg/_reset_session/close`、`VoiceAgent._wire_live` 以及 `tests/test_conversation_window.py`、`tests/test_gemini_session_lifecycle.py`、`tests/test_live_turn_lifecycle.py`、`tests/test_astrbot_live_context.py`。当前实现仍在后端内协调概率/窗口许可；策略实例由行为注入，MemoryStore/ContextBridge 写入已全部移出生成。完整提议/许可与有界 dispatcher 仍待落地，不能称为本文全部边界已成立。

首个切片使用现有 JSON 消息夹具构造真实适配的隔离试验：reader/dispatcher/闸门保留真实代码，替换网络和行为端口，断言未获批输出为零、ID/修订不串轮、字节上限、晚转录兼容、关闭无悬空任务。直接记忆写入已移除；后续继续迁出后端中的策略协调。该试验通过才开始替换现有 Live 通路；真实模型与房间的时序/延迟仍须按语音联调验收实测，不用夹具通过冒充供应商验证。

当前撤销能力 `detach_completed_input` 原子移交已完成缓存轮次的用户事实并丢弃助手缓存，重复取出为空。晚转录窗口仍由生成判断：窗口内到达修订可拼回该完成轮，过期后的片段不附到已撤销轮；尚未turnComplete的收集片段不作为完成输入交付。取消、来源刷新与换绑的真实消息回归见 `tests/test_voice_history_outcomes.py`。
