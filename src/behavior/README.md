# 行为决策

本模块已拥有接话决策、输入尝试/终态关联、Live 提议许可、最新输入/冷却、抢话许可和自动串门。接口以 [api.py](api.py)、[models.py](models.py) 和 [工厂](__init__.py) 为准。全局职责见 [系统架构](../../ARCHITECTURE.md)。

## 职责与导航

行为回答“下一步是否行动及行动意图是什么”，隐藏关键词、概率、成员窗口、额度、冷却及串门阶段。真实连接/房间/播放归渠道，识别和表达归生成，历史/授权归上下文。行为不持 SDK、模型、播放资源或其他模块的内部状态。

| 能力 | 独有知识和状态 | 入口 |
| --- | --- | --- |
| 接话与回合 | 稳定触发、尝试、决定、Live 等待、行动终态 | [replies](./_internal/replies/README.md) |
| 串门与调度 | 候选、耐久日额度、访问阶段、问候/告别、手动优先 | [visits](./_internal/visits/README.md) |

这两项按知识分别封装。接话用同步的具名决策/反馈；串门保留内部异步深任务，通过注入 `VisitsHost` 窄执行端口完成阶段。没有强制所有能力实现同名 `decide`，也不把串门等待/重试机械拆成 `advance` 并迫使应用驱动私有状态机。两者不直接互相导入；应用按真实会话传递通知，串门退出阶段经端口约束新自动输入。

## 公开调用

`create_behavior()` 返回 `Behavior`：

1. `start_session(SessionRef)` 登记渠道确认的会话。重复登记不清额度；真实新会话清掉原窗口、尝试和行动关联。
2. `admit(AudioReady, limit)` 在首次识别 I/O 前，按原会话和触发 ID 幂等计入尝试；同键不同触发拒绝，旧会话不能消耗当前容量。0 表示不限额，空识别、失败、沉默与控制不退款。
3. `decide(RecognitionObserved, ReplySettings)` 验证原始触发和已登记许可，再返回 `NoAction`、`Speak` 或 `ControlAction`。过期许可不能开始新表达，表达截止不越过许可期限。配置和控制候选是解析后的具名结构，生成供应商不拥有接话策略。
4. `decide_live(LiveProposal, ReplySettings)` 根据原提议、版本、说话者和硬截止返回 `Speak/NoAction/DeferLive`；同一轮一次概率抽样。许可阶段拒绝经 `NotExecuted` 结算后重新评估，也保留原随机选择。
5. `feedback` 接收实际 `ActionOutcome/PlaybackResult` 或执行前 `NotExecuted`，`finish_attempt` 记录具名处理结束。回执区分 accepted/duplicate/conflict/unknown；不同会话的结果不能占用当前行动终态。Attempt ID 编码完整 SessionRef，旧代次不能与新输入同名。

单槽输入、冷却和连续抢话通过独立 `create_reply_scheduler/create_barge_in_gate` 提供；应用只传递处理结束和语音活动，不持有内部窗口/时长表。`create_reply_policy` 提供成员策略的有限查询入口；尝试额度只经 `Behavior.admit(AudioReady)`，返回时许可已绑定输入、会话与有限期限，不能先构造不完整许可再补字段。

## 自动串门的取舍

`create_auto_visits/create_visit_store` 已迁入现有全部串门能力。配置递归冻结，持久状态保留原格式；访问预留/确认、问候、停留、等待当前回复、告别、有限退房重试及冷却都由行为拥有。`VisitsHost` 只授予必要执行和查询，不开放 SDK 或整个应用配置；调用者无需理解串门 phase。具体边界和故障见 [visits](./_internal/visits/README.md)。

主动开启全新话题从来不是现有产品能力，本次重构不新增该功能。将来实现时沿“定时情境→行为意图→上下文准备→生成→渠道执行”扩展，不能把已有串门/问候说成自主聊天，也不为它预建未使用接口。

## 验证与维护

执行 `python -m pytest -q tests/behavior tests/test_conversation_window.py tests/test_voice_reply_probability.py tests/test_voice_turn_limit.py tests/test_auto_visit_controller.py tests/test_auto_visit_state.py`。公开反例覆盖改触发冒用许可、过期许可、旧会话/同 session_id 不同绑定、错误执行反馈、重复终态、许可失败后重复抽样。真实应用测试经外部 socket/音频验证窗口、概率、逐轮抢话与换房，双 bot 按 [验收指南](../../docs/bot-pair-acceptance.md) 另行执行。

允许依赖 `context.models`、`channels.models` 和公共机制；不导入其他模块实现。内部规则变更保持在本模块，接口/状态归属变化同时维护本页和相应子模块说明。

阿里/DeepSeek 的 `RecognitionObserved.reply_assessment` 携带绑定原会话/输入的语义候选。行为以叫名优先、addressed/contribute允许、silent/invalid/unavailable沉默决定，不再抽样或用固定窗口强制接话；已命名短语可绕过普通最短时长。无语义候选的后端保留原策略。验证见 `tests/behavior/test_semantic_replies.py`。

`Speak.reason` 提供已授权意图的具名原因供控制台关联：语义路径区分keyword、semantic_addressed、semantic_contribute；它不改变许可与执行语义，也不包含模型推理。
