# 渠道交互

当前状态：**房间控制、目录与音频能力已实现，完整动作/事件协议仍待迁移**。已实现契约以 [api.py](api.py)、[models.py](models.py) 和 [工厂](__init__.py) 为准；下文其余能力仍为待实现目标。全局规则见 [系统架构](../../ARCHITECTURE.md)，目录约束见 [代码要求](../../docs/coding-standards.md#模块目录与接口导航)。

`create_playback(channel, session, duplex)` 提供迁移期单次 `Playback`：拥有提交证据、静音抑制、打断与三十秒排空确认，应用只消费不可变 `PlaybackResult`。它不包含生成选择、历史可提交性或完整 Play 动作台账；取消与原会话校验见 [公开契约](api.py)。

## 职责与非职责

提供“感知平台变化、在正确会话执行动作”的能力，隐藏 SDK、身份映射、浏览器音频、连接重绑和播放代次。这里是实际连接、房间和输出状态的唯一权威。识别语义归对话生成；是否接话、自动串门归行为决策；历史与投递归上下文。OneBot/AstrBot 的文字收发是外部代理通路，不在渠道中再造文字大脑。

## 内部能力地图

| 能力 | 隐藏的知识与状态 | 当前迁移入口 |
| --- | --- | --- |
| 平台与文字适配 | Oopz/OneBot 编解码、身份映射、连接重试；保留现有映射库 | `src/bridge/`、`src/oopz/`、`src/onebot_v11/` |
| [房间会话](_internal/session/README.md) | bot 绑定、真实房间、会话代次、进退房互斥、断连清理 | `VoiceAgent.join/leave`、`BridgeController` |
| [音频收发与播放](_internal/output/README.md) | PCM 格式、采集窗口、输出背压、静音和音量、流结束确认 | `create_duplex` / `create_output_controls` / `create_vad`，实现位于内部 output；SDK voice 适配 |
| 动作执行台账 | 动作去重、执行前置条件、终态与取消、可查询结果 | `Channel.execute/outcome` 及 `models.RoomOperation`；`VoiceAgent` 仍有高层流程协调 |

采集端负责音频质量和分段边界，不解释自然语言；无人时长等策略从语音活动观察计算。SDK 内状态是适配实现，模块外不再读取它构造第二份真实状态。

## 已实现入口与迁移差距

现有语音入口通过 `Channel.execute` 执行进退房，`snapshot/outcome` 查询状态及动作结果；`push_audio` 是过渡期绑定原会话的音频入口。真实状态由内部 [RoomChannel](_internal/session/service.py) 唯一维护，`VoiceAgent` 的旧房间属性仅做只读兼容投影。原有 HTTP/串门入口暂经 VoiceAgent 编排，不要求调用者提前改用未实现的完整命令集。

平台目录现提供独立的 `PlatformDirectory`，通过工厂 `create_oopz_directory(bot)` 绑定一个账号；`joined_areas` 和 `voice_members` 把 SDK 字典/对象别名解析为不可变的域、房间和成员观察。`confirmed` 的空结果与 `unavailable`、`invalid_response` 分开；外部错误正文不会交给调用者。SDK 兼容归一化通过非序列化的 `source_validated` 保留原始成员响应的可信性，目录拒绝被容错清洗掩盖的畸形数据。成员的 `is_self` 和 `is_bot` 在平台边界计算，无法识别的机器人标记为 `None`，不作为真人候选。目录不拥有实际房间状态，也不提供 SDK 对象。自动串门和独处退出只消费这些观察，进房前仍重新查询，并在异步查询和预留后核对目录绑定身份，拒绝旧账号的迟到观察；未知成员状态重置独处窗口。调用及畸形响应验证见 [目录契约测试](../../tests/channels/test_directory_contract.py)。

首个切片已覆盖旧会话拒绝、重复动作、排队取消、进房取消补偿、退房失败、在途推送屏障、换绑迟到结果和不合作传输超时；调用示例及验收见 [渠道契约测试](../../tests/channels/test_room_contract.py)。运行 `python -m pytest tests/channels -q`。

仍待迁移：bot/duplex 异步换绑及释放、手动抢占调度、VoiceAgent 的高层操作锁/控制代次、完整 Play/SendText 及事件流。当前不支持显式 `cancel`；不宣称跨重启去重、完整帧格式校验或统一播放终态。`push_audio` 成功只表示一次传输提交；不替代下一批的播放终态。

## 后续公开契约

- `events() -> AsyncIterator[ChannelEvent]`：应用运行时是唯一消费方，再按事件种类分发；不让多个模块争抢一个迭代器。事件含 ID、会话、来源序号和时间；音频经有界通路交给生成能力，不写入普通历史快照。断连明确发布状态变化；控制事件不能按音频丢帧策略静默丢弃。
- 已实现的快照与确认标记见 `api.py`/`models.py`；`uncertain` 禁止新进房和音频，旧 HTTP `joined` 仅保留最近进房未获退房确认的兼容含义。
- 已实现的 `execute` 当前只接收 JoinRoom/LeaveRoom；后续增加文字、完整播放和输出控制命令，并扩展效果/进度结果，不能把草案变体当成已有实现。
- 后续增加显式取消及有界 CancellationPending 契约；当前 `outcome` 已区分运行、终态与未知，查询保留/容量语义以公开代码为准。

`Scope`、`SessionRef`、`ChannelCommand`、`OutputFrame`、`ActionOutcome` 由 `channels.models` 定义。房间目标与已进入的会话是不同类型；进房指向目标并携带预期状态版本，播放/退房绑定已观察会话。文本和音频输出分别建模，音频帧带编码、采样率和声道；首批播放流只接受音频帧，文字发送使用独立命令，避免一条混合流隐含跨媒介执行顺序。渠道不依赖模型供应商事件。

## 生命周期与故障

会话子模块拥有房间锁，输出子模块拥有播放任务，执行器负责动作台账与结果；应用无需取得锁或操作 epoch。手动房间控制先使旧会话动作失效，再停止输出并收尾；每次推流也验证绑定会话。过期动作在消费流前拒绝并关闭流；中途失效关闭流、停止播放，不能把余帧送入新房间。`execute` 返回前关闭其拥有的输出迭代器；调用方取消时以有限等待完成清理，记录可查询终态，再传播 `CancelledError`。

输出流正常结束表示生产者正常结束；生成失败终态、缺失正常终态或异常不能转换为普通流结束。生成模块的公开输出适配将其转换为渠道公开的 `OutputSourceError`（保留关联 ID，隐藏供应商异常），执行器返回失败/部分执行，并保留已知输出范围；取消仍传播 `CancelledError`。类型落地时用重载或不同具名命令约束“播放必有流、其他动作无流”，同时在构造/边界检查非法组合。

`completed` 对音频只表示传输适配可确认的播放终结，不声称人实际听到；缺少确认时返回未知范围或失败。结果携带可核实的已发送文本/音频进度，未能确认的转录不作为已输出历史。ActionOutcome 仅由渠道产生；生成/上下文在渠道执行前失败由上层独立反馈表示，禁止伪造渠道终态。断连/设备错误不自动重播已可能出声的内容。终态通过返回值和事件均可到达，接收者按动作 ID 去重。

## 依赖与验证

不导入其余四模块、`app` 或 WebUI；只依赖公共技术机制及内部平台适配。旧代码暂由 `_internal/legacy.py` 桥接，SDK 只在渠道适配访问。外部机器人回复通过绑定路由送出，不能再触发本地生成。

现有证据入口：`tests/test_voice_operations.py`、`tests/test_voice_duplex_rebind.py`、`tests/test_voice_output_controls.py`、`tests/test_tts_completion.py`、`tests/test_bridge_restart.py`。迁入后在 `tests/channels/` 补公开接口行为：重复动作、手动优先、退房失败、切房迟到帧、满队列与取消；当前测试尚不证明目标接口成立。

## 平台适配与执行器的详细边界

平台适配仍作为本模块内部能力，按现有复杂度先用 `_internal/adapters/` 的简单文件组织，不为每个协议机械建子目录。平台适配只拥有账号连接、协议编解码及数据库身份映射；房间真相和动作结果分别归会话和执行器。Oopz SDK、OneBot、HTTP/AstrBot 回执不是可互换协议，不能共用一个万能 dict callback。

| 内部能力/端口（待实现） | 输入与输出约束 | 允许了解的知识 |
| --- | --- | --- |
| `PlatformConnection.start/stop/status/events` | 返回带 binding ID 的连接状态/已解析平台事件；关闭后不再发布该 binding 的有效观察 | bot、认证、WS 重连、供应商 ID |
| `TextGateway.send(SendText) -> TextSendResult` | 返回平台消息 ID/明确未知投递，不自动重试可能已成功的发送 | OneBot/Oopz 格式转换与映射；不决定回复内容 |
| `EventNormalizer.parse(raw: object) -> ChannelEvent | InputRejected` | 保留已验证范围/来源 ID/序号/发生时间；未知载荷不转成“空消息” | 平台格式；不能依赖 context/behavior 类型 |
| `CommandExecutor.execute/cancel/outcome` | 接任务式命令，维护台账，调用会话/播放/文字端口后形成 ActionOutcome | 动作标识、幂等、执行阶段和结果；不了解模型和配额 |

`ChannelEvent` 包含 `event_id, scope, binding_id, source_sequence, occurred_at, observed_at, payload`；载荷是消息、成员、连接、音频/语音活动、房间转换或动作终态的具名分支，不要求每类事件都带房间 SessionRef。文字路径保留 `origin`、`causation_id`、`conversation_owner=external_astrbot`，外部回复按原路由发送，不能再解释成新用户输入。无法提供可信平台序号时，适配分配本地 binding 内序号，不声称外部全局有序。

### 命令、台账及结果字段

所有命令带 `action_id, source, deadline, causation_id`，并可带 `parent_action_id` 关联同一次发言意图；单调时钟截止仅在本进程有效，外部期限在解析边界换算并限制。进房分支带目标与预期 room revision；退房/播放/输出调整带 SessionRef；文字分支带已解析文字目标与非空内容。自动请求的“不能替换已在房”是执行安全前置条件，不让调用者设置任意布尔绕过。不同变体禁止无关字段，合法对象由构造器保证。

执行台账暂用 `_internal/execution.py` 简单文件，不另建子模块。`ActionEntry` 保存命令的规范化指纹、状态、执行任务引用、原会话、开始/结束时间和结果；不将有状态流对象算进指纹。播放命令另带由生产者分配的稳定 `output_id`，同 action ID 配不同 output ID 冲突，避免两条不同流被误当重试。

每个真实渠道命令拥有独立 action ID。默认一次意图只执行一种主输出；兼容 `reply_text_to_channel` 时，补充 SendText 使用新 action ID 并关联主意图，独立记录发送与音频播放结果，不能因一项成功伪报另一项成功。

登记在首次 await 之前完成；同 ID/指纹重入等待同一任务，不再次消费新流，新传入未消费流由调用入口关闭。任一重复等待者被取消，只取消该等待，不撤销原执行；显式 cancel 或首个执行拥有者取消才撤销执行。活动项上限为 128，台账总项（活动及终态）上限 1024；登记时同时预留最终结果的位置，满载返回 Busy。终态保留至少 10 分钟，达到总容量后优先驱逐已过期条目，否则拒绝新动作，不静默破坏保留承诺。其中进退房台账的容量及保留期已由首个切片实现；其他命令及事件语义仍为目标，后续按实际需要调整有限预算。`process_run_id` 防止重启复用同动作标识；台账丢失时查询 Unknown，恢复不自动重放有副作用动作。

状态为 `Running(stage)` 或终态；阶段区分等待执行、转换房间、传输、排空、清理。动作终态含 `action_id, scope/session_ref, source, started_at, finished_at, status, reason, effect, progress`：status 区分完成/拒绝/过期/取消/失败/部分执行；effect 区分未执行/已知执行/不确定；progress 保存文字消息 ID 或音频提交/确认范围。失败来源和安全原因用具名枚举，SDK 异常详情只留脱敏日志。

`cancel` 请求幂等并等待有限收尾；超过接口等待期限返回 CancellationPending，任务仍受清理总截止约束，最终效果不确定时形成有明确不确定性的失败/部分结果，不无限悬挂。若 cancel 自身被取消，已发出的撤销意图不撤回，由台账保留清理任务，调用者用 outcome 查询 Running(cleanup) 或终态。执行结果冻结后再向事件出口发布；终态事件和 execute 返回值是同一事实，不生成第二个动作。

### 内部依赖与事件背压

工厂装配执行器、会话、音频和平台实现。执行器依赖各自窄端口；输出只依赖会话声明的只读 guard/收尾端口类型；会话只依赖自身声明的 VoiceBinding/PlaybackRetirer 类型，经注入调用播放器，不导入其具体实现。平台适配实现这些端口，不让会话或输出导入 SDK。平台与会话都能向渠道事件出口提交事实，事件出口不调用消费者业务。

控制/终态与音频使用分开的有界队列，由单一 events 消费器优先取控制，再公平取音频。首批控制队列容量 256；状态更新可按 binding 合并成最新快照，已完成动作结果仍留台账。控制满载时停止受理新动作并明确降级，不按音频规则静默丢控制；终态可由台账查询补读，正常停机不等待消费者腾空。消费者须在流关闭/溢出信号后重取 snapshot 和自己持有 action ID 的 outcome；这不是持久化事件总线，不承诺进程崩溃后事件可靠重放。音频容量与断点语义见子模块文档。

## 子模块设计自查与开工切片

- 去层：去掉会话会让每个调用者承担传输绑定、失效和未知房间；去掉输出会让级联/Live 各写播放确认/清理。执行台账当前用一个文件，不再增加只转发的 service 层。
- 替换：换浏览器或 SDK 只改平台/音频适配和能力测试；调用者仍提交命令。若语音编码能力改变，明确调整公开帧契约，不偷偷在 app 写供应商条件。
- 故障推演：旧 push 未返回时新 join 必须等待收尾或隔离旧绑定；退房失败保留未知目标；源失败不当正常 EOS；未知取消不伪造终态。详见两个子模块状态机。
- 首批先迁房间/binding 唯一拥有者和必要的最小输出屏障，再迁完整播放/事件，最后收敛文字适配。每批迁完调用者才删旧字段；没有新 API 测试不能宣称边界完成。

首个切片已实现有限台账、房间状态和超时屏障；上述完整命令集、事件背压与播放进度仍未实现，不能把当前测试当作完整设计已通过。模块整体测试入口见上文，具体故障矩阵见子模块。

## 实现与阅读顺序

迁入后依次阅读 `api.py` → `models.py` → 所需 `_internal/` 实现。内部能力是职责划分，不要求立即创建同名目录、类或文档；具有独立状态和维护需求时再增加子模块说明。跨模块只使用公开契约，工厂由应用装配，禁止返回内部可变对象。迁移批次与验收见 [重构准备计划](../../docs/superpowers/plans/2026-11-08-module-refactor-readiness.md)。

### 本批音频迁移

音频采集、200 帧有界队列、输出音量/静音和 VAD 已从旧目录迁入 `_internal/output/`；使用 [公开接口](api.py) 与包工厂。采集回调及队列按 capture epoch 隔离，关闭后清理旧帧；重启不能投递旧回调。相对音量策略由行为解析，通过 `OutputControls.adjust` 在共享输出控制锁内取得当前音量，保存成功与音频应用结果分别报告。配置读写只经注入保存端口。

验证：`tests/channels/test_duplex_lifecycle.py`，以及原 `test_voice_output_controls.py` / `test_tts_completion.py` / `test_vad.py`；完整播放动作与帧台账仍是内部目标设计。
