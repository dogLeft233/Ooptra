# 行为决策

当前状态：**部分实现**。现有尝试额度、接话策略、单槽信箱/冷却、抢话许可和串门调度/台账已迁入；统一决策/反馈和主动话题仍为目标设计。已实现契约见 [公开接口](api.py)、[结果类型](models.py) 与 [能力工厂](__init__.py)，以下完整决策签名仍不可直接调用。全局规则见 [系统架构](../../ARCHITECTURE.md)，目录约束见 [代码要求](../../docs/coding-standards.md#模块目录与接口导航)。

## 职责与非职责

提供“根据触发和情境，决定是否及如何行动”的能力，隐藏接话概率、关键词、冷却、配额、主动发言、自动串门与手动优先策略。真实房间和播放归渠道；识别/表达归生成；人格/历史归上下文。这里不直接访问 SDK、模型和数据库格式。

## 子模块导航与依赖

| 子模块 | 独有知识和状态 | 详细设计 |
| --- | --- | --- |
| 接话策略与回合额度 | 关键词/概率/窗口、最新输入、冷却、级联预留、Live有限等待 | [replies](./_internal/replies/README.md) |
| 串门与主动调度 | 候选与日额度、耐久访问、问候/告别阶段、手动覆盖与唤醒 | [visits](./_internal/visits/README.md) |

两个子模块互不导入，behavior协调器按触发类型分派并仲裁同一会话的动作冲突。协调器只保存 action ID→拥有子模块的关联，并把visits只读VisitAdmission(Open/Blocked)传给replies；不复制额度、冷却或串门阶段；房间真相通过具名渠道事实观察。先按知识分这两块，不建立逐个转发的policy/service/repository层。

拆出统一万能“配额服务”会让串门持久日额度和进房级联尝试共享无意义抽象；保留各自计费语义及拥有者，只统一反馈关联。模型语义候选归公开行为类型，但候选解释与执行授权不等价。

## 已实现入口与迁移边界

`create_cascade_attempts()` 返回会话级尝试额度拥有者。新渠道会话建立后登记一次，在首次模型等待前申请许可；失败、空识别和沉默不退款。旧会话不能消耗新会话额度，同一会话重复登记不会清零。返回不可变许可/拒绝及只读快照，不向调用者开放计数写入。

`create_reply_policy()` 返回概率选择、关键词规范化及有界成员窗口能力。级联与 Live 后端已直接使用该入口，原 `voice_agent/reply_policy.py` 已移除；供应商或传输变更不需要重新实现这些算法。时钟和随机源可以注入，模块不导入旧后端或配置对象。

当前额度每次申请代表一个新输入；尚未实现目标触发 ID 去重、AttemptFinished 及完整行动台账；现有冷却已有独立调度能力。Live 的一次抽样缓存、迟到转录等待和回合协调仍在生成内部适配，不宣称已经完成完整决策模块。

验证入口：`python -m pytest tests/behavior tests/test_conversation_window.py tests/test_voice_reply_probability.py tests/test_voice_turn_limit.py -q`。公开额度测试覆盖耗尽、失败不退款语义、同会话登记与旧会话拒绝；现有级联/Live 回归验证关键词窗口、抽样与换房行为。真实双机器人验收由应用层按 [验收指南](../../docs/bot-pair-acceptance.md) 执行。

## 待实现公开契约

- `decide(trigger: BehaviorTrigger, context: SituationSnapshot) -> Decision`：显式触发为消息、`AudioReady`、`RecognitionReady/RecognitionFailed`、定时、人工请求、`ChannelChanged`、Live 输出提议或 `LiveControlProposed`；返回 `NoAction(reason)`、`ProcessInput(AttemptPermit)`、`Speak(intent)`、`DeferLive(proposal_id, revision, until)` 或具名渠道命令。渠道变化触发更新会话/手动覆盖事实，不伪造用户输入。按作用范围串行保留决策状态；同一触发 ID 不重复预留额度。
- `feedback(feedback: BehaviorFeedback) -> FeedbackReceipt`：具名分支 `ExecutionFinished(ActionOutcome)`、`NotExecuted(action_id, session_ref, stage, reason)` 和 `AttemptFinished(attempt_id, handled_at, end_reason)`，按动作/尝试 ID 幂等更新，返回 Applied/Duplicate/Unknown/Conflict具名回执及关联ID；NotExecuted覆盖准备失败、执行前过期等，不伪造渠道执行结果。失败、部分执行、取消分别处理，不读取渠道私有字段。反馈产生后继串门动作时，由snapshot给出唤醒并经下一次decide取出，不隐藏调用渠道的副作用。
- `configure(policy: BehaviorPolicy) -> ApplyReceipt`、`snapshot() -> BehaviorSnapshot`：按版本应用策略和查询只读调度状态。长等待不放进 `decide`；定时器按给定下次唤醒时间产生触发，策略仍在模块内部。

`Decision`、`SpeechIntent`、`BehaviorTrigger` 和策略类型归 `behavior.models`。`SpeechIntent` 具名区分回应、主动话题、固定台词，含 action ID、scope、原会话、有效期、输入引用和策略约束；进退房等结果可引用 `channels.models` 的命令。`NoAction`含reason和可选attempt_ref：已admit后直接概率沉默携原attempt，未admit拒绝不携attempt；不创建表达生成任务，已受理的级联输入仍通过 AttemptFinished 收尾。`DeferLive`仅等待有界的新信息，不创建action或预留额度，最终拒绝/超时后不可复活。人工命令同样受安全约束，是否消耗策略配额在命令来源分支中明确，避免多个调用者猜规则。

## 最小数据与协调

`AttemptPermit` 含 `attempt_id, input_ref, session_ref, policy_version, expires_at`，由行为模块登记后发给一次输入流程；不是模型/HTTP可自造的授权。`BehaviorTrigger` 包含稳定 `trigger_id`、scope/原会话、来源、发生/到达时间和具名载荷；Live提议另含proposal ID、revision及最晚截止。`LiveControlProposed`将生成端ControlProposed机械映射为trigger_id、ControlCandidate和opaque origin/tool_ref，不引用dialogue类型；按origin幂等决策，渠道实际结果由应用回送生成工具端口。后续识别/控制/发言结果携同attempt_id，任何结束路径都由应用finally交一次AttemptFinished：沉默/空识别/错误也结束尝试，当前尝试冷却只在此更新，ExecutionFinished不重复计费/设置冷却。`ControlInterpretation` 区分已解析的 NoControl/LeaveCandidate/VolumeCandidate 与分类不可用/非法，规则见 replies。`BehaviorSnapshot`只读返回策略版本、下次唤醒、暂停/待确认原因，不暴露可变计数表。

每个动作在一个子模块登记与终结，`feedback`的返回/事件重复由相同ID去重。同范围入口串行仲裁，但不持锁等待跨模块I/O。定时触发带generation，旧唤醒不能唤活新会话；`decide(ChannelChanged, situation)`更新内部观察并使旧意图失效；应用收到同一渠道事件时按原会话范围取消其流程，不需要读取behavior内部action表。

## 生命周期与故障

级联输入在 `AudioReady` 通过 admission 后返回 `ProcessInput(AttemptPermit)`，在首次 ASR/分类 I/O 前按 input_ref 幂等计入一次尝试；其后空识别、控制、概率沉默和失败均不退款，RecognitionReady 到 Speak 不再扣额。不能改成“只有决定表达才扣”或成功播放计数。每次真实新进房按渠道事件重置该会话额度；重复进房事件不重复重置。一般回复冷却按当前实际规则区分完成与打断，串门计数保留未确认任务恢复机制。

手动控制使待执行自动意图作废；渠道仍独立复查实际会话。尚未执行的意图也有过期终态，由运行时递交反馈，不永久占用待执行名额。关闭停止调度并终结/持久化未确认意图；持久化失败暂停自动行为，不能猜配额后继续运行。自主发言为目标能力，不能把已有自动串门称为已实现主动话题。

## 依赖与验证

允许 `context.models`、`channels.models` 和公共机制；不依赖两者实现、对话生成或系统维护。时钟、随机源和状态存储由工厂注入，本模块定义所需窄端口，不创建跨全库的万能存储接口。

现有证据：`tests/test_auto_visit_controller.py`、`tests/test_auto_visit_state.py`、`tests/test_round_throttle.py`、`tests/test_voice_turn_limit.py`、`tests/test_voice_reply_probability.py`。迁入后 `tests/behavior/` 用可控时钟/随机源验证沉默不生成、预留不超额、重复反馈、手动优先和主动触发；旧集成覆盖保留；新接口验证见本页末尾及内部模块导航。

## 实现与阅读顺序

迁入后依次阅读 `api.py` → `models.py` → 所需 `_internal/` 实现。内部能力是职责划分，不要求立即创建同名目录、类或文档；具有独立状态和维护需求时再增加子模块说明。跨模块只使用公开契约，工厂由应用装配，禁止返回内部可变对象。迁移批次与验收见 [重构准备计划](../../docs/superpowers/plans/2026-11-08-module-refactor-readiness.md)。

### 本批迁移状态

原串门控制器、配置解析和持久访问台账已迁入 `_internal/visits/`，通过 `create_auto_visits` / `create_visit_store` 装配。`VisitsHost` 是应用注入的窄执行端口，不暴露 SDK 或整个 VoiceAgent 配置；检查计划、问候/告别重试、冷却和持久化预留只有此模块拥有。当前仍为异步控制器驱动端口，尚非完整意图/反馈状态机。

最新输入信箱/冷却与抢话许可已迁入 `_internal/replies/`；接口见 [api.py](api.py)，公开验证见 `tests/behavior/test_reply_scheduler.py`、`test_speech_interrupt_policy.py` 和 `test_visit_service.py`。外部配置快照不能原地启用串门；未知进房结果保留预留并暂停。
