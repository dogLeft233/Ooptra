# 接话策略与回合额度

状态：部分实现。级联会话尝试额度在 [attempts.py](attempts.py)，概率/关键词及成员窗口在 [policy.py](policy.py)；最新输入/冷却已进入 [scheduler.py](scheduler.py)，连续抢话许可进入 [barge_in.py](barge_in.py)；幂等反馈与 Live 提议协调仍为目标契约。入口见 [行为决策](../../README.md)。将“是否回应、等待哪句、预留哪次尝试、何时可再次回应”作为一项能力，隐藏概率、关键词、窗口、冷却和未完成意图，避免调用者拼装多步检查。

## 已实现调度与抢话能力

`create_reply_scheduler` 拥有单槽最新输入、冷却期限及代次；`reset` 使旧输入和旧反馈失效，并唤醒尚在等待旧冷却的消费者。旧等待者在触碰新 pending 前复查代次；取消等待回收定时器，保留随后新提交的输入。单个消费者驱动公开 `next/finish`，任务取消和模型生命周期由应用负责。

`create_barge_in_gate` 接受已确认 `SpeechActivity`，拥有逐成员连续发声时长和本轮最多一次的许可。安静活动清理对应成员累计；`rearm` 清理所有累计。0 毫秒保持第一帧立即抢话的兼容规则。策略不持有原始 PCM、渠道或模型；实际停止播放由应用根据许可协调。状态最多保留 256 个近期成员，避免来源标识异常增长。

公开验证：`tests/behavior/test_reply_scheduler.py`（最新替换、旧代次不能吞新输入、重置立即解除旧冷却、取消释放等待）、`tests/behavior/test_speech_interrupt_policy.py`（逐成员连续发声、静音重置、每轮一次、输入校验）。旧集成验证见 `tests/test_round_throttle.py`、`tests/test_barge_in_gate.py`。

## 类型与内部接口

| 类型 | 字段与不变量 |
| --- | --- |
| `ReplyInput` | `trigger_id, attempt_ref, input_ref/proposal_id, proposal_revision, session_ref, speaker_ref, recognized_text, mode, source, offered_at`；文字未知不是空文本，模型失败不是 NoControl |
| `ControlCandidate` | `NoControl`、`LeaveCandidate(input_ref)`、`VolumeCandidate(input_ref, parsed_volume_command)`；仅是候选，不执行SDK。`ControlInterpretation` 另含 `InterpretationUnavailable/Invalid`，禁止分类失败悄悄当普通聊天 |
| `AttemptPermit` | `attempt_id, input_ref, session_ref, policy_version, expires_at`；AudioReady通过admission时创建并扣级联额度，同input_ref重入返回原许可，不重新消费 |
| `ReplyReservation` | `action_id, trigger_id, attempt_ref(可无), session_ref, policy_version, reserved_at, expires_at, source, state`；表达action不再独立扣输入额度，同触发重复不再抽随机数 |
| `ReplySessionState` | 会话引用、每次进房级联 attempts、冷却deadline、成员对话窗口、pending最新输入引用、有限decision缓存；无PCM、bot或模型连接 |

- `consider(input: ReplyInput, situation: SituationSnapshot, policy: ReplyPolicy, admission: VisitAdmission, now: ClockSample) -> ReplyChoice`：完成输入admission/策略判定及必要额度计入，返回 ProcessInput/Silence/Speak/Control/Defer；不得让调用方依次 call can_reply/increment/build_intent。
- `settle(feedback: BehaviorFeedback, now: ClockSample) -> Settlement`：终结本模块拥有的动作或输入尝试，返回内部Applied/Duplicate/Unknown/Conflict；模块入口映射为公开FeedbackReceipt，未知动作不改变当前额度。
- `on_channel_change(event: ChannelChanged) -> ReplyInvalidations`：按渠道确认事实重置新会话状态、使旧意图失效；返回待取消 action IDs，但自身不操作渠道/模型。
- `next_wakeup() -> Wakeup | None`：给出冷却或延期提议下一检查时间，应用只驱动定时触发，不解释冷却规则。

`VisitAdmission`为behavior内部共享的只读联合：Open/Blocked(reason,until)。由visits拥有的admission端口产生，经behavior协调器传入；replies只执行准入结果，不读取或缓存串门phase，两个子模块不相互导入。

这些端口只供 behavior 内部协调器使用；跨模块使用 behavior 公共 `decide/feedback`。

## 策略顺序与额度

1. 检查触发是否属于本地能力，渠道会话/观察是否适用；外部 AstrBot 所有的文字输入返回具名沉默。
2. 手动请求和明确控制候选按授权/策略分别处理；安全/会话约束不能被强制关键词绕过。
3. AudioReady先检查启用、公告/占用、级联额度、冷却与输入新鲜度，通过后在任何ASR/分类I/O前登记AttemptPermit并计入一次输入尝试。随后RecognitionReady/RecognitionFailed、控制候选和最终Speak/NoAction均关联同attempt，不重复扣费。配置沿用现有默认和有效范围，人工固定台词和Live不扣级联输入额度。
4. 冷却等待采用最新输入引用覆盖旧引用；队列容量一，替换旧输入发出 superseded 结束事实。超时丢弃，不积累无人消费PCM。识别完成若已换新输入，同样不得回应旧句。
5. 额度计算的是进入旧backend的输入尝试，**不是发言次数**：空识别、ASR失败、控制、概率沉默、表达失败也消耗。最终NoAction/NoSpeech没有action，但仍有AttemptFinished。每次新渠道会话重新计数；排队尚未admit、过期拒绝、公告阻挡不扣，DeferLive不扣。若按旧无控制/无关键词策略可在ASR前直接概率沉默，这个已admit尝试也已消耗，并以带attempt_ref的NoAction收尾。

当前 `_throttled_round` 对未被打断的处理结束会设置冷却，即使不一定输出成功；迁移不得未经说明改成“只有成功播放才冷却”。目标在 `AttemptFinished(attempt_id, handled_at, end_reason)` 显式保留此语义；打断清零，旧会话反馈不能更改新会话deadline。落地前用现有 throttle 测试和新增失败/沉默场景锁定兼容，而非凭“完整说完”注释猜行为。

窗口按已知成员区分，未知成员不获得窗口；沿用现有最多128个成员窗口，过期清理。概率使用注入随机源，同一输入许可/触发决定缓存至原会话有效期，容量满时拒绝/明确淘汰已过期项，不能丢掉活跃预留后重复扣额。

## Live 有限等待

Live 提议可能先有音频、后有转录。`DeferLive(proposal_id, revision, until)` 仅用于尚未许可提议；没有 action、没有配额预留。应用映射为生成端 Hold；生成端给出绝对最晚截止，行为不可延长。相同提议只有递增 revision 的新信息才重评，等待期间同版本返回原决定；关键词确认后才创建唯一 Speak/action。

NoAction 映射最终 Reject、超时终结后不能因迟到转录重新复活。保留现有有限迟到关键词窗口（当前 Live 为2秒），不要将 Hold 改成任意等待。提议被手动控制或切房作废即终结；生成端有界缓冲，行为不保留音频。

生成许可可能在行为决策后才被拒绝：app对未接受的Allow（StaleProposal/Expired/Conflict/Closed）交 `NotExecuted(stage=permission)` 终结原action，再按新revision重新decide；不能遗留预留或把旧action绑新提议。同一已接受Allow的重复 resolve 必须返回原action绑定/已接受，不交NotExecuted错误终结活跃动作。Live不扣级联输入额度，但仍需该action收尾。

## 状态、取消与故障

输入尝试为 `Admitted(AttemptPermit) → Recognizing → Deciding → Finished`；可选表达动作为 `Reserved → Finished`，Live未许可提议另有Deferred。RecognitionFailed、NoSpeech、NoAction、控制动作和表达完成/失败均在应用流程finally提交AttemptFinished，按attempt ID幂等设置冷却；动作ExecutionFinished只更新action状态，不重复计费或设置输入冷却。动作可以结束为实际执行终态，或 `NotExecuted`（准备失败、生成前失败、过期、被替换、停止）。后者只释放pending占用，不回退已消耗输入尝试；已经可能出声的失败必须用真实执行反馈，不伪装未执行。

重复同终态返回 Duplicate；相同 action/attempt 冲突终态返回 Conflict/诊断，经公开FeedbackReceipt可见，不再次设置冷却。触发 ID只在原会话生效；旧会话feedback可收尾旧动作，不改变当前窗口/额度。进程重启后的新房间会话不恢复旧回复动作，持久历史由上下文负责；未完成动作按原会话终结/未知，不主动重播。

## 切片、依赖与自查

内部只用公共情境/渠道类型和时钟、随机源，不导入 visits/生成/存储。behavior协调器负责将具名结果交另一模块，无SDK回调。对概率/关键词算法保持简单函数，不为其单建目录或service层。

迁移前定位证据：原 `reply_policy.allow_reply/ConversationWindow`（现已迁至本模块）、`VoiceAgent._round_worker/_throttled_round/_handle_utterance_reply/_cascade_limit_reached`；测试 `tests/test_round_throttle.py`、`tests/test_voice_turn_limit.py`、`tests/test_voice_reply_probability.py`、`tests/test_conversation_window.py`。先迁额度与反馈，保留旧入口适配；再迁窗口/冷却/概率，最后迁Live许可策略。

目标验证：并发不得超额；重复触发/反馈不重复消耗；失败/已admit沉默不退款；首次ASR前计入、识别到表达不重复扣；延期不表达；旧会话终态不延长新会话冷却；最新输入获胜；Hold到期后迟到转录不复活。更改冷却策略只改此子模块和策略配置，不需改渠道/生成。去掉本层会把预留/冷却规则散回多个入口，因此保留；完整反馈/冷却协调仍是设计分析；已落地额度的公开行为测试见 `tests/behavior/test_attempt_budget.py`，现有关键词/概率/窗口回归已经改用行为模块。
