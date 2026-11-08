# 接话策略与回合额度

公开入口见 [行为接口](../../api.py)，协调实现在 [decisions.py](decisions.py)。本能力隐藏稳定输入的准入/结算、接话规则、Live 有限等待、成员窗口、最新输入与冷却、连续抢话；不持模型、SDK 或播放对象。

## 真实实现地图

| 实现 | 状态与规则 |
| --- | --- |
| `decisions.py` | 触发/许可登记、决定缓存、Live 一轮一次抽样、行动与实际终态关联 |
| `attempts.py` | 当前真实会话和不可退款尝试次数 |
| `policy.py` | 关键词规范化、最多128成员窗口、概率抽样 |
| `scheduler.py` | 单消费最新输入信箱、冷却与代次；旧等待不能吞新输入 |
| `barge_in.py` | 逐成员连续语音、每轮最多一次抢话许可；最多256个近期成员 |

## 级联顺序与幂等

应用收到完整输入先用稳定 `AudioReady` 经 `admit` 预留，再开始识别。相同会话+触发返回原许可，异内容拒绝；旧会话先拒绝，不污染当前去重容量。单个会话最多登记1024个稳定触发，容量满明确拒绝，不忘记活跃触发后重新计费。

`RecognitionObserved` 必须携带原始触发与已登记许可；验证在决定缓存读取之前，不能借相同 attempt ID 更换说话者、会话或许可。许可在构造时即保留输入ID、正整数次数/版本和有限期限，不存在待补字段的有效许可；默认60秒，过期不能创建表达；已受理输入的失败、空识别、控制、概率沉默均消耗原尝试。Speak 不再扣额，表达截止取许可期限和策略期限较早者。

`AttemptFinished` 是本次输入处理的终态，重复/冲突均可见；旧会话或未知许可返回 unknown。`feedback` 只终结其登记 action，真实结果还需匹配原 SessionRef。`NotExecuted` 用于尚未交渠道的失败，不能伪造渠道结果；播放已开始时必须回传渠道事实。

`ReplyScheduler.finish` 拥有冷却推进：未被打断的处理结束沿用现有冷却，即使沉默或失败；打断不设置冷却。reset 使旧输入和冷却无效，立即唤醒旧等待者；旧等待者在读取新信箱前复查代次。这样尝试终态与异步调度资源各只有一个拥有者，应用不操作内部表。

## Live 提议

`LiveProposal` 绑定原会话、proposal ID、revision 和生成方硬截止。无许可时返回有界 `DeferLive`，应用只映射 Hold；不消费级联额度、不接收音频缓存。过期提议不启动表达，旧会话先拒绝且不消耗随机数。

每轮首次抽样固定，递增新信息可按关键词重评；同版本返回原决定，已终结 NoAction 不因迟到转录复活。生成许可因旧 revision/版本而拒绝时，`NotExecuted(stage=permission)` 终结原 action，下一次评估保留原随机选择。已接受的重复 Allow 返回原 action，不误交 NotExecuted。

说话者不明确仍可由显式关键词回应，但不得给猜测的成员开启窗口。窗口按成员区分，未知成员、换房和过期都不能继承另一人的窗口。

## 抢话与验证

`BargeInGate.observe(SpeechActivity)` 只消费已解析的活动事实，连续时长按成员累计，安静重置；`rearm` 清理旧时长。0毫秒保留第一帧立即抢话规则。实际停止模型/播放由公开任务能力执行，不在策略中回调 SDK。

`tests/behavior/test_public_value_guards.py` 验证非法布尔开关、NaN时长/窗口、内部许可标记与动作身份不能进入决策；`test_attempt_budget.py` 直接通过 `Behavior.admit` 验证额度和完整许可。

`tests/behavior/test_decision_contract.py` 对同触发计一次、许可伪造/过期、完整会话 ID、旧反馈、一次抽样和错误执行结果做公开接口反例；`test_reply_scheduler.py/test_speech_interrupt_policy.py` 验证资源与策略。`tests/test_barge_in_gate.py/test_live_turn_lifecycle.py/test_voice_reply_probability.py` 使用真实应用和生成任务、替换外部 socket/音频传输验证集成，不依赖旧供应商回调或手填私有计数。

语义接话通过 `ReplyAssessment` 输入同一个 `decide`，不另建模型客户端或行动台账；绑定错误在构造识别事实时拒绝。明确叫名优先于模型沉默/失败，普通候选不抽样、不开启固定窗口，去重、许可期限和控制授权仍生效。验证见 `tests/behavior/test_semantic_replies.py`。
