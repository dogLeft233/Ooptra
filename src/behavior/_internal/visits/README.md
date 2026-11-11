# 串门与主动调度

状态：目标内部子模块，尚无 Python 实现。入口见 [行为决策](../../README.md)。集中拥有调度时钟、候选房间、串门配额、问候/告别阶段、手动覆盖与主动触发；通过意图推进，绝不自己进房或播放。

## 类型与内部接口

| 类型 | 字段与不变量 |
| --- | --- |
| `VisitState` | `visit_id, area, planned_target, observed_session, phase, leave_deadline, pending_action_ids`；observed_session只由渠道事实确认，不因计划而成为当前房间 |
| `VisitLedger` | 配额日期、全局/分域计数、pending预留、confirmed访问、全局/分域冷却；延续原状态格式与ID含义 |
| `Wakeup` | `timer_id, generation, not_before, reason`；过期generation触发无效，定时任务不持有SDK |
| `AutonomyPolicy` | 串门启用/域候选/间隔/日额度、独处退出、主动话题开关与冷却；未实现主动话题默认禁用，不把已有串门等同于自主聊天 |

- `advance(trigger, situation, now) -> VisitChoice`：输入配置/定时/成员/手动事实/执行反馈，返回无动作、单一意图和下一唤醒；内部完成候选选择及需要的耐久预留。长等待用下一触发，不在此阻塞 sleep。
- `admission(session_ref: SessionRef) -> VisitAdmission`：只读返回 `Open` 或 `Blocked(reason, until)`；在WaitingReply/Farewell/Leaving等退出阶段阻止新自动回合，不能由调用者从phase字符串猜规则。协调器把此结论传replies，不复制串门阶段。
- `settle(feedback: BehaviorFeedback, now) -> VisitChoice`：按 action ID 推进阶段，产生后继意图；不会直接调用生成/渠道。当前问候、等待回复、告别由状态阶段表达，不将整套流程散在 app 条件分支。
- `apply_policy(policy, expected_version) -> ApplyReceipt`、`pause(reason)`、`resume(reconciled_state)`：配置版本冲突明确拒绝；恢复前重新加载并核对未确认串门，不一键清空pending。
- 存储窄端口 `reserve_visit/confirm_visit/rollback_visit/set_cooldown/load_snapshot`：由本子模块适配原 `AutoVisitStateStore`，写成功后才推进内存状态或发出有副作用意图。实现可继续使用原原子JSON，不因上下文选SQLite而改串门存储。

## 状态机与动作序列

主状态 `Disabled/Paused/Waiting/Cooldown`；活跃访问 `Reserved → Joining → Greeting → Staying → WaitingReply → Farewell → Leaving → Ended`。行为控制阶段，执行中状态只作观察；实际进退房结果仍以渠道为准。

首次满足日额度与冷却才 durable reserve；进房成功确认 visit 并增加全局/分域计数，重复确认幂等。确定未执行或确定进房失败且没有部分成功时 rollback预留；执行结果未知不能退款并继续串门，转 Paused 等待对账。重启恢复 pending同理，不根据超时猜“肯定没进去”。配额日期沿用北京时间日切，回拨不能产生额外额度；持久UTC截止恢复后转相对单调时间，进程内不受墙钟跳变操控。

问候成功或失败都明确处理后进入 Staying；到离房条件先禁止新自动回合，等待当前回复有限收尾，再告别/退房。等待返回 `NoAction(waiting)` 并在snapshot给出有截止的Wakeup；渠道执行终态或Timer触发再次advance。发言/退房返回既定意图，app只执行并回传结果；不新增WaitAction，app不解释等待/重试政策；“是否告别、何时放弃等待、是否重试”由此状态机判断。串门离房重试保留现有有限节奏（0/2/5秒），渠道只处理单个执行尝试，不能两层各自重试放大副作用。

手动控制到来提升调度代次，使旧自动意图失效；观察到的手动房间仍可应用既有独处退出策略，不能因串门开关关闭就忽略这条路径。手动退房按原域冷却，自动退房按全局冷却。独处退出需已确认成员信息持续满足原30秒窗口；未知/缺失成员快照不等于独处，退房失败后重新确认窗口再重试。

## 反馈、并发和持久化故障

同作用范围由 behavior入口串行调用；网络执行在模块外，不持状态锁等待生成/播放。Action ID映射到当前阶段，重复结果不发两次告别；旧访问结果可完成原台账，但不能让新手动房间进入 Leaving。进房反馈与 ChannelChanged事实双到达时按 visit/action/session三者对齐，不能算两次。

写盘失败保留上一已提交状态并暂停自动行为，向公开snapshot给明确错误；不能以空计数继续。shutdown停新调度，保留已持久化未确认访问；应用取消执行后须回传 NotExecuted或真实执行结果，无法确认时保留待对账。时钟、随机源、存储故障均可注入测试，不向调用者暴露文件锁。

## 迁移、依赖与验证

当前证据：`AutoVisitController.on_operation/_can_join/_finish_visit`、`AutoVisitStateStore.reserve/confirm/rollback`、`auto_visit_settings.effective_area`。现有测试：`tests/test_auto_visit_controller.py`、`tests/test_auto_visit_state.py`、`tests/test_auto_visit_integration.py`、`tests/test_empty_room_status.py`。当前控制器直接访问agent并编排台词，迁移必须逐条改成上面的阶段输出，不能直接搬文件就称边界成立。

先保留状态文件适配并迁 durable预留/确认和房间命令，再迁问候/告别阶段、独处退出；主动话题是后续真实功能，先定义明确关闭的触发分支，不预建人格/话题插件框架。replies与visits互不导入；behavior协调器仲裁冲突，只共享公开策略/反馈类型。后台定时驱动允许在此内部实现，也可由app按 Wakeup驱动，业务时间判断只在这里，不能两处各存调度状态。

目标测试：预留失败不进房、确认失败暂停、未知进房不得退款、日切/时钟回拨、重启pending暂停、手动优先、旧告别/退房不能影响新会话、重复反馈只推进一次、独处未知不退出、有限重试和停止回收。调整串门策略只改本子模块，不改app/渠道；存储适配变化也不影响replies。当前为设计与调用路径核对，未证明目标状态机已运行。
