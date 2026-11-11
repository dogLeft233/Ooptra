# 应用装配与流程

状态：待实现的支撑层设计，非第六个业务模块；当前入口为 `main.py`、`BridgeController` 与 `VoiceRuntime`。全局职责/静态依赖见 [系统架构](../../ARCHITECTURE.md)，目标接口入口见五模块导航。迁移时按实际需要增加 `assembly.py`、`flows.py`、`management.py`；现在不创建空 Python 文件。

## 拥有与禁止拥有的知识

拥有模块实例装配、同一作用范围内的流程任务、输入路由、因果标识和管理入口到公共能力的适配。可以根据公开的能力分支选择级联/Live/外部文字流程。不得持有房间锁、SDK bot、模型参数、人格/outbox可变对象或策略计数；业务失败的重试范围由各模块决定，不在这里统一无限重试。

输入路由以已解析的 `conversation_owner` 与事件类型分支：外部 AstrBot 文字只走渠道代理，语音走本地流程。一个输入只能有一个对话拥有者，重连不改变同一输入的路由；不能将外部机器人回复再次作为本地触发。

## 流程状态与端口

| 局部数据（待实现） | 字段和允许的操作 |
| --- | --- |
| `FlowKey` | 进程 run ID、scope、原 SessionRef 和触发 ID；作为任务关联，不是实际房间真相 |
| `FlowTask` | task 引用、只读触发/意图/准备结果、可选 attempt_ref、动作 ID 与是否开始执行；有停止和异常观测，结束后释放引用 |
| `LiveFlow` | 固定 SessionRef、公开 LiveSession 端口、事件任务/输入任务、每轮执行任务；不复制模型轮次状态机 |
| `AppReadiness` | 当前启动身份、bootstrap 就绪和外部连接降级汇总；仅查询各拥有者快照 |

任务注册表按作用范围限制并发；业务模块决定 Busy/NoAction/替换意图等语义，应用不能悄悄把无界并发变成默认。首次迁移保持现有并发行为并用测试固定，再按明确策略改变。子任务由流程对象创建和 await，Python 3.10 不依赖 TaskGroup。完成回调观测异常但不吞掉业务失败。

入口调用的是应用任务操作 `handle_event`、`handle_request`、`stop_scope` 等有实际用例的内部函数，不需要通用 Workflow/EventBus 基类。模块实例不通过全局 service locator 或可变万能 context 流转。

## 跨模块连接规则

1. 渠道事件只被应用消费一次，先递交上下文观察，再把更新后的只读情境交行为；渠道终态回馈使用动作 ID 去重。音频采集是另一条有界通路，不放入普通历史快照。
2. 级联先由行为根据 AudioReady 返回 ProcessInput(AttemptPermit)，按输入尝试预留，再识别、必要时独立解释控制候选并决策；保持沉默不启动表达生成。准备/生成在渠道受理前失败，用行为的 `NotExecuted` 反馈；渠道开始执行后，用 ExecutionFinished 携其真实 ActionOutcome 反馈，不能伪造另一份渠道结果。无论 NoSpeech、沉默、控制、失败或取消，均用 AttemptFinished 按原 attempt 结束本次处理/结算冷却，Speak 不重复扣额。
3. 生成的音频使用 dialogue 公开的单消费输出适配交渠道执行；生成终态解释、序号、背压和关闭下沉生成模块，渠道负责消费/传输终结。应用不再实现供应商分片算法或逐帧播放器。
4. 实时会话持续接收输入，提议事件交行为产生 Allow/Hold/Reject。应用只传许可，不解释关键词、不决定等待时长或重放。过期许可由生成端拒绝，不能凭 app 当前字段强行复活轮次。已登记 Allow 若未被接受，递交 NotExecuted(stage=permission) 收尾；已接受的重复许可返回原绑定，不能误终结活跃动作。Live 控制工具候选交行为和渠道，结果经 resolve_control 返回原工具调用；旧连接结果不影响新连接。
5. 显式记忆工具通过 dialogue 声明的 `ContextToolPort` 注入适配，映射到 context 的公开查询/写入；来源绑定来自 PreparedContext，模型参数不能指定任意人格或跨会话来源。适配是窄类型转换，不承载记忆路由和重试策略。
6. 渠道会话变化先交拥有者确保旧输出不能触及新传输，再取消并 await 旧流程；即使应用取消迟到也由渠道保持安全。新旧流程按 SessionRef 分离，旧流程 finally 不能关闭新对象。

`ActionOutcome` 是渠道事实；`BehaviorFeedback` 是行为待执行状态的反馈载荷，两者不混用。历史记录只消费已确认表达证据；文本发送与音频播放是不同动作，音频失败不能因文字成功而被当成完整语音完成。具体双输出字段和证据规则由渠道/生成/上下文契约定义，应用只关联和传递。

## 管理面与停止顺序

`management.py` 适配 operations 声明的 `RuntimeControl`：把合法配置转换成模块配置并汇总逐模块应用回执；限制新业务请求/停调度；撤销并等待现有流程；关闭生成会话、渠道传输和上下文工作者；汇总是否安全停机。实际资源归各模块，失败回执保留未关闭清单。上下文/行为需要落盘的状态必须在其存储关闭前完成。

quiesce 保留管理交接通路；成功后 operations 持久化 handoff，再通过 `RuntimeControl.request_exit(handoff_ref)` 通知主循环退出，主循环最后关闭 WebUI 并结束进程。父进程确认真实退出才接管切换。quiesce 部分失败返回资源清单并保持 blocked/degraded，不把已关渠道的空壳进程当旧服务正常；恢复需显式重启原部署或经验证的恢复流程。

候选进程启动时读取持久化维护任务和角色；父未写 complete/rolled-back 时，只开放查询/health，配置、维护及业务副作用入口保持受限，自动行为暂停。记录损坏返回恢复诊断，不用新的内存 gate 自动开放写入。bootstrap-ready 可以表示本地装配/监督就绪，不表示维护已交还业务激活许可。

维护就绪与外部连接健康分开。WebUI 已响应不等于 bootstrap 完成；Oopz 暂离线不使候选版本自动失败。配置保存成功也不等于运行时全部应用成功。

## 迁移和验证入口

先迁移 `main`/`VoiceRuntime` 中为第一条房间控制路径必需的装配；不把整个 `VoiceAgent` 原样改名为 AppRuntime。随后用实际切片增加级联/Live流程，旧入口只留响应/协议兼容适配。

集成测试放 `tests/integration/`，使用五模块公开端口替身与模块行为测试互补，不能只断言内部方法调用次数。需覆盖：文字/语音单一路由、准备失败释放行为名额、切房旧流程清理、重复终态不重复记录、生成流关闭级联、维护停止失败不执行切换。现有入口证据测试包括 `tests/test_auto_visit_integration.py`、`tests/test_voice_operations.py`、`tests/test_voice_settings_reload.py`；新端口与公开契约仍待实现验证。
