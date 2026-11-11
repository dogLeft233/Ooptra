# 五模块边界与接口演练

状态：2026-11-08 目标设计分析；不表示新接口已实现。当前地图与全局不变量见 [系统架构](../../../ARCHITECTURE.md)，接口契约分别归五模块 README，本文只记录取舍和使用推演。后续落地改变契约时同步所属模块，避免在此维护第二份 API。

## 设计目标与比较

目标是让一次修改只需理解所属模块的知识和少量公开契约，而不是把 `VoiceAgent` 拆成多个互相读取字段的对象。

| 边界问题 | 可行替代 | 本次选择及代价 |
| --- | --- | --- |
| 把房间与播放留在语音总控，还是统一渠道执行 | 留在总控迁移较少，但 WebUI、串门和生成持续依赖锁、bot 与代次 | 渠道拥有房间/播放及执行台账；调用方提交任务并等待结果。先迁移最小房间路径，不能一次拆掉全部总控 |
| 行为与生成合并，还是分开 | 合并方便 Live 直接开口，但冷却/主动策略难以独立测试和更换 | 分开，行为授权、生成表达。级联增加识别入口；Live 增加输出许可闸门，须适配试验验证延迟与供应商事件 |
| 上下文变成全局可变容器，还是快照/准备/记录能力 | 大容器调用短，却泄漏存储、来源和实时状态 | 只读快照与任务式上下文；渠道仍复查状态。允许有版本差距，接受重新准备/决策，而非跨模块大锁 |
| 五模块互相调用，还是应用装配传递 | 互调减少流程代码，却容易循环导入及重复收尾 | 模块仅引用规定的公开类型，`app` 管流程任务和传递；不引入通用事件总线。策略、重试和资源收尾仍留模块 |
| 维护继续属于 WebUI，还是独立能力 | 留路由中改动少，但启动器和热更新反向依赖 HTTP 层 | 维护定义管理端口，应用注入；配置应用明确部分失败。父进程职责保留，不引入微服务 |

五模块不是按步骤拆开的五个透传层：渠道隐藏传输和执行一致性，上下文隐藏来源与投递，行为隐藏策略状态，生成隐藏供应商与流，维护隐藏部署事务。若将来某层只剩转发且没有这些知识，应再次合并或缩小，而非为既定目录保留空抽象。

## 只读契约的调用演练

以下是**伪代码**，只使用模块公开能力，不导入 `_internal/`；不作为可以运行的示例。所需操作和结果分别由模块 README 定义。所有错误分支都要落在拥有者的契约中，应用只协调。

### 房间控制：第一条重构路径

```text
HTTP 入口认证并把 object 解析为合法 Join/Leave 请求
  → 应用取得渠道快照，构造带 action_id / 预期版本的 ChannelCommand
  → await channels.execute(command)
  → 得到完成 / 冲突 / 失败 / 取消等 ActionOutcome
  → context.record(对应实际输出记录)；behavior.feedback(ExecutionFinished(outcome))
  → 将具名结果转换为旧 HTTP 响应，保留客户端兼容
```

失败无需读取 bot：断连、目标不合法、状态版本冲突由接口返回。HTTP 请求取消是否撤销动作由入口策略明确：若选择撤销，就取消并等待流程清理；若返回任务引用继续执行，必须提供查询入口，不能遗留无人观察任务。第一批沿用可等待完成的房间操作，暂不新建通用任务框架。并发手动/自动请求的优先级由来源语义和渠道执行约束解决，不由调用者获得锁解决。

### 级联接话及固定台词

```text
ChannelEvent(audio) → context.observe/snapshot → behavior.decide(AudioReady)
  → ProcessInput(AttemptPermit)：首次处理 I/O 前按 input_ref 预留尝试
  → dialogue.recognize → Recognition
  → context.observe(具名识别观察) → context.snapshot(scope)
  → behavior.decide(RecognitionReady/Failed，携同 attempt，situation)
  → NoAction：结束；或 Speak：context.prepare(意图及输入引用)
  → dialogue.generate(intent, prepared) 产生受控输出流
  → channels.execute(播放命令, 输出帧迭代器)
  → 确认后的表达及 outcome → context.record；ExecutionFinished(outcome) → behavior.feedback
  → AttemptFinished(attempt_id, handled_at, end_reason) 结算处理结束与冷却
```

固定台词由意图携带文字，跳过 ASR 和自由回复生成，只使用已声明的表达能力。主动话题用 Timer 触发同一决策流程，不伪造用户。已获准的级联尝试无论空识别、沉默、控制或失败均消耗次数，后续 Speak 沿用原尝试不再次扣额；NoAction、失败和取消也提交 AttemptFinished，不能因为没有 action 漏结处理冷却。准备失败或生成在执行前失败时，应用递交 NotExecuted(action_id, session_ref, stage, reason) 交行为释放待执行状态，不能伪造渠道播放成功；上下文只记录实际观察。

生成流直接交 `dialogue.audio_output(events)` 得到 GeneratedAudio，应用把其 frames 交渠道消费，执行结束读取只读 evidence。该公开适配隐藏正常/失败终态、按完整采样切分与连续帧编号、部分证据和关闭级联；不把算法放在 app。渠道负责消费/关闭，生成负责模型资源清理；停止消费的 `aclose` 必须触达底层任务，落地用故障测试证明。

生成失败终态或缺失正常终态转换为渠道公开的输出源失败，不能变成普通 EOS 导致误报完成。渠道提前拒绝/关闭后 evidence 立即返回已观察部分及未知范围，不等待无人消费的生成终态。补充文字是独立 SendText action，以 parent_action_id 关联意图，分别记录结果；文字成功不能替代音频完成。

### 切房与取消

```text
原会话 S1 正在生成/播放 → 手动加入新房间
  → 渠道使 S1 失效并清理旧输出 → 加入成功产生 S2
  → 应用取消 S1 流程并等待；上下文观察新会话；行为反馈旧动作
  → 迟到 S1 音频 / 生成终态 / 外部记忆回执均按 S1 的原绑定处理
```

即使应用取消消息迟到，渠道也必须阻止 S1 帧进入 S2。旧 outbox 可以按原绑定完成写入，但不能覆盖新来源；外部重复回执不重复记录。清理失败应报告退房/连接不确定，禁止直接继续假设已空房。`CancelledError` 由模块清理后传播，应用收尾可查动作终态，并按 ID 去重返回值与终态事件。

### Live 持续会话

```text
已加入 S1 → context.prepare → dialogue.open_live(固定 S1 的上下文)
  → 持续送入 S1 音频
  → 识别观察交 context；TurnProposed/Updated 交 behavior.decide
  → DeferLive / Speak / NoAction 转为 resolve_turn(Hold / Allow / Reject)；只把获批输出接到渠道执行
  → 每轮执行终态记录及反馈；切房/改来源关闭旧 Live 端口并重建
```

Hold 仅在提议的有限截止内等待更多信息，不释放音频、不预留动作额度；拒绝/超时后不能复活。Allow 在行为登记 action 后若被生成端以 stale/expired/conflict/closed 拒绝，应用递交 NotExecuted(stage=permission) 收尾；同一已接受许可的重复回执返回原绑定，不能误当拒绝而终结活跃动作。接话策略可以在没有最终转录时使用语音活动和情境，不要求 Live 等待级联识别。模型可先计算，但未批准输出不能离开生成模块。人工台词与模型自动轮次必须分别关联动作，不能产生两个并发播放器。当前 `SessionStarter`、`StreamingUtterance` 等能力并不等于已实现上述端口；真实适配是独立开工门槛。若供应商无法满足许可/有界输出，保留明确能力限制或调整契约，不隐藏在 `app` 特判。

### 控制与记忆工具

级联 recognize 后可独立 interpret_control，Live 则发 ControlProposed。候选 → 行为授权 → 渠道执行或拒绝 → Live resolve_control 回传具名结果；工具任务不能直达 SDK，旧连接结果不发到新 socket，reader 不等待命令执行。模型输出的候选不是授权。

记忆工具由生成声明 ContextToolPort，装配将类型化请求转给上下文公开 recall/remember。PreparedContext 给出 context_ref 和基于真实已观察输入/管理员策略签发的授权引用；模型只提供 query/content，不能选择来源或构造授权。记忆入队与已确认分开，来源过期/授权不足/满载均明确失败。历史已提交但未受理投递时，同记录的幂等重试按上下文契约补投，不重写历史或重播。

### 外部文字与维护

文字：Oopz 输入的路由标记为 AstrBot 所有，渠道做 OneBot 适配并接收回复，实际发送后形成记录；本地行为/生成不再响应这一输入。上下文同步轮询是另一条协议，不把 OneBot 和 HTTP 回执混成一次完成。

维护：入口解析 → `operations.plan/execute` → JobRef；HTTP 超时只影响等待。维护通过注入端口停调度、撤销流程、关闭渠道、持久化上下文，成功后持久化交接引用并通知主循环退出，父确认旧进程退出后接管；重启任务状态由 `job` 恢复查询。候选 checking 期间禁止配置和新业务副作用，父提交后再激活；quiesce 部分失败保持 blocked/degraded，不伪报旧服务正常。配置保存和热应用各自报告结果；不能因为 HTTP 返回成功就认为所有模块已应用。

## 变化场景验证：设计分析

以下核对了当前入口与拟迁移调用方，未做替换实现，不是运行通过结论。

| 变化与预期 | 当前扩散证据 | 目标接口如何限制扩散 |
| --- | --- | --- |
| 更换共同能力不变的生成供应商：应限于生成适配及装配配置 | `VoiceAgent` 目前使用 `StreamingUtterance`、`hasattr(tts/speak_text)` 及 Live 特判，后端持有 context bridge | 生成公开能力/流约束；装配选择实现，渠道只见输出帧，上下文通过 PreparedContext 提供；能力改变时允许明确改契约 |
| 调整冷却/级联额度：应限行为策略和测试 | `VoiceAgent._cascade_limit_reached/_handle_utterance_reply` 与 `AutoVisitController` 分散计数 | 行为在尝试的首次识别/分类 I/O 前预留，同 attempt 后续不重复扣，分别反馈动作与尝试结束；渠道不解释额度，对话不查策略配置 |
| 更换本地历史或 outbox 存储：应限上下文及兼容迁移 | `MemoryStore` 与 `ContextBridge` 分别持有历史与待写，后端读取人格/上下文 | 使用 snapshot/prepare/record/delivery；行为和生成不持有存储句柄；既有数据迁移另有兼容测试 |
| 替换房间/播放适配：应限渠道 | `VoiceAgent` 操作 bot/duplex，`VoiceRuntime` 负责重绑，调用方依赖执行代次 | 公开会话引用和任务式 execute，重绑、代次、确认、锁封装在渠道；平台能力差异显式报告 |
| 去掉 WebUI，保留配置/维护：应不改变业务 | `VoiceRuntime._save_output_volume` 导入 `webui.config_editor`；MaintenanceService 与路由同文件 | 维护 API 与管理端口独立 HTTP；WebUI 可替换；父进程查询持久化任务而非 HTTP 对象 |

自查结果：职责和静态类型依赖可形成有向无环关系；五模块各有独有知识，当前不需要第六模块。公开契约已补 snapshot、动作查询/取消、识别入口和 Live 会话能力。设计可指导分批实施，但跨流关闭、Live 输出许可和配置部分应用仍需实现验证，详见 [准备计划](../plans/2026-11-08-module-refactor-readiness.md)。
