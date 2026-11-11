# 房间会话与传输绑定

状态：**首个切片部分实现**，代码见 [service.py](service.py)。房间事实、串行转换、去重查询、取消补偿和音频屏障已迁入；bot/duplex 生命周期、高层控制锁/代次及手动抢占尚在旧路径。本页状态机及端口中的未实现部分继续作为目标。属于 [渠道交互](../../README.md)，不新增跨大模块访问入口。未来 Python 接口和状态按此设计落地；当前行为仍在 `VoiceAgent`、`VoiceRuntime` 与 SDK。本页是会话内部契约的唯一文档定义，落地后以代码为准。

## 能力、取舍与状态

提供“在可确认的传输上持有唯一有效房间会话，并安全切换”的任务能力。调用者不参与锁、代次及浏览器身份比较。保留在 VoiceAgent 中成本较小，但模型、串门和 WebUI 都须知道这些规则；因此迁为独立拥有者。播放算法不放这里，通过本页定义的窄收尾端口协调。

| 私有状态 | 拥有者与不变量 |
| --- | --- |
| `binding_id`、`connection_state`、当前 VoiceBinding | 会话服务；每次不同物理 bot/transport 绑定生成新 binding ID，同一实例重复绑定不重建；SDK 对象只留内部适配 |
| `room_state`、`active_session`、`room_revision` | 会话服务；最多一个 ActiveSession；revision 在可观察状态变化时单调增加，SessionRef 在每次成功建立的新会话改变，即使房间相同 |
| `transition_task`、`transition_id`、串行转换锁 | 会话服务；同一时刻最多一项房间网络转换，转换 ID 防止迟到网络结果覆盖当前状态 |
| `admission_generation` 与失效标记 | 会话服务；停止/切房/换绑先禁用旧会话的新输出，再收尾；不对外发布可修改计数器 |
| `cleanup_tasks` 与隔离绑定 | 会话服务；所有清理有任务引用、总截止时间及异常观测；失败绑定不得再用于新房间 |

公开 `SessionRef` 只是 `(process_run_id, binding_id, session_id, room_target)` 的不可变身份，不是可执行 SDK 句柄，也不证明当前有效。`RoomTarget` 由已解析非空平台/area/channel 构造；连接就绪与已在房是不同事实。

## 内部端口草案

以下仅供本模块使用，首批实现时写入本子模块的代码，不在父层复制签名：

- `RoomSessions.join(request: JoinRequest) -> RoomTransition`：内部使用已登记动作、目标、来源、预期 revision 和截止时间；重复动作由外层执行台账拦截。自动请求不能覆盖现有人工会话，手动请求可以替换旧会话。
- `RoomSessions.leave(request: LeaveRequest) -> RoomTransition`：必须绑定 SessionRef；旧会话请求在任何清理之前拒绝，不能先停新房播放再检查。
- `RoomSessions.replace_binding(binding: VoiceBinding | None) -> BindingTransition`：异步禁用并清理旧绑定后安装新绑定；无连接时不伪造已成功退房，状态按已知事实和不确定性表达。
- `RoomSessions.snapshot() -> RoomView`、`RoomSessions.is_current(ref: SessionRef) -> bool`：同步只读；后者仅用于本事件循环内部快速检查，不能代替 I/O 前后及切换屏障。
- `PlaybackRetirer.retire(session: SessionRef, reason, deadline) -> RetireResult`：由会话子模块声明、播放实现提供，停止旧输出并等待在途 push/stop 收尾，不持会话短状态锁调用。
- `VoiceBinding.join(target, deadline) -> JoinedBinding`、`leave(session, deadline) -> LeaveConfirmation`、`close(deadline) -> CloseConfirmation`：平台适配实现；未知效果明确返回，不能把超时当无副作用。

`RoomTransition` 内含原状态、新状态、可确认的进退房事实、故障来源及效果确定性。切房旧房成功退出但新房加入失败时保留两段事实；不能只发一个“join 失败”掩盖已退房。用户看到的 ActionOutcome 由渠道执行器基于这些事实形成。

## 状态机与转换规则

```text
Disconnected --绑定就绪--> Idle --join--> Joining --确认--> Joined(S)
Joined(S) --leave/replace--> Retiring(S) --输出收尾--> Leaving --确认--> Idle
Idle --join失败且确认未进房--> Idle
Joining/Leaving/Retiring --效果不明或清理失败--> Uncertain(原目标/绑定)
Uncertain --核实退出/关闭旧绑定--> Idle 或 Disconnected
任意状态 --停机/换绑--> Closing --确认释放--> Disconnected
```

失联、退房失败后的房间事实保留最近已知目标和确定性，不假称 Idle。Uncertain 状态拒绝新进房/播放，只接受核实、退房恢复或关闭；重试失败留在 Uncertain。目标状态不能为了兼容旧 `joined: bool` 退化为不可解释布尔值，旧 HTTP 适配同时返回错误/降级信息。

成功进房只证明渠道会话建立；上下文准备或模型启动失败由应用发撤销/退房命令，不由本子模块导入上下文或模型。第一批过渡必须迁走对应最小清理调用，禁止会话服务回调整个旧 VoiceAgent。

## 并发与取消：顺序不可省略

1. 房间转换由转换锁串行。短状态锁只保护状态/revision/任务指针，持有时不执行 SDK、发布事件、等待播放器或运行调用者回调。
2. 手动控制到达时先由同事件循环的入口标记更早自动转换被取代、取消其等待并推进失效标记。正在执行的外部 I/O 仍须按未知效果收尾；不能承诺远端请求能瞬间取消。手动请求随后等待转换锁；新的自动请求不插队，不抢占人工房间。
3. 对当前 S 执行退房/替换：短锁内标记 Retiring、禁止新输出并保存旧资源引用；释放短锁，通知输出子模块停止/收尾；**等待成功屏障后**才调用旧绑定 leave 或新 join。屏障失败就隔离/关闭旧绑定，不在同一浏览器继续进新房。
4. 所有 push/stop 都绑定旧 binding ID。开始 push 前检查会话，I/O 完成后再核对；开始前的检查不能防住 await 中切房。新房间不得在旧 push 未收尾且未隔离时复用传输。
5. join 取消或超时可能已进房，必须在有限清理期限内核实/退房；未知则 Uncertain。记录执行台账状态后传播调用方的 `CancelledError`。多次取消不能打断必需清理；保留独立清理任务并可通过动作 ID 查询收尾状态。
6. 事件在释放状态锁后进入渠道事件通道；消费者慢不能阻止关闭。清理任务绝不等待需要同一转换锁才能结束的业务流程，模型任务的停止由应用负责。

网络动作、输出收尾、清理分别有受解析配置约束的正有限超时；首次实现冻结默认值并加入超时测试，不复用无总截止的逐步超时。SDK 无法证明已停止的旧浏览器由适配器关闭隔离，不能通过“忽略异常继续进房”恢复。

## 迁移与验证

现有证据：`VoiceAgent.bind_bot/_drop_stale_duplex/join/leave/_leave_locked`、`VoiceRuntime.bind_bot/unbind_bot`。现有回归分别验证同 transport 重绑保持、旧访问不能退新房、退房失败不发成功、回调在锁外、reply worker 不使退房死锁。

最小切片：先冻结旧 API 响应 → 建立公开范围/会话/命令与结果类型 → 迁 room/binding 唯一拥有者及必要 output retire 屏障 → 旧 VoiceAgent/自动串门/HTTP 改用公开渠道 → 删除旧可写房间/epoch 字段。若旧输出仍能绕过屏障，第一批不能完成，须把最小绑定播放入口一起迁入。

待增加到 `tests/channels/` 的公开行为测试：同房重入的新 SessionRef、旧动作不能清新房、leave 失败保留未知状态、join 在提交后取消、阻塞 push 期间手动切房、stop 失败隔离旧 transport、重复绑定不误关闭、关闭和事件背压不互锁。外部 transport 用可控 Future/失败替身，不以读取私有锁值作为唯一断言。当前测试不是新状态机已实现证明。
