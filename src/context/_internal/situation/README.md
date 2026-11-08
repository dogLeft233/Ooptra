# 情境投影

状态：目标内部子模块，尚无 Python 实现。上层入口见 [上下文管理](../../README.md)。本模块隐藏观察排序、去重和情境裁剪，不保存 SDK 对象或长期记忆，不控制房间。

## 内部类型与入口

以下名称是待实现内部契约，外部模块不得导入；落地后定义移入本地类型/接口及 docstring。

| 数据 | 必需字段与不变量 |
| --- | --- |
| `ObservationKey` | `source_id, connection_generation, source_sequence`；来源无序号时使用稳定事件 ID，不用接收时间猜全局顺序 |
| `ProjectionRevision` | `session_ref, observation_version`；同会话单调递增，重启后的新来源代次与旧代次不同 |
| `RoomProjection` | 已观察 `SessionRef`、渠道状态版本、成员摘要、语音活动时间、新鲜度、连接状态；真实空房与观测未知分别建模 |
| `RecognizedInput` | `input_ref, session_ref, speaker_ref, occurred_at, text, recognition_state`；空文本、未识别及失败分别表达，身份未知不猜测 |

- `apply(observation: Observation) -> ProjectionUpdate`：单事件循环内原子修改，返回 `Applied(revision)`、`Duplicate` 或 `Stale`。只有 Applied 增加版本。
- `capture(scope: Scope, now: Instant) -> SituationSlice`：返回不可变投影及当前版本；时间仅用于算新鲜度，不在读取时伪造连接事件。
- `activate(session_event: ChannelSessionChanged) -> ProjectionUpdate`：只接受渠道事实中的新会话；进房命令已受理不能激活会话。会话改变清理房间局部成员/活动，保留上一会话的历史归档引用。

普通 `apply` 统一分派具名事件到 `activate`，外部不需要先 activate 再 apply。两个名字是内部实现分工，不是双重公开调用。

## 状态与顺序

状态：`Unknown → Observed(session,version) → Stale/Disconnected`；渠道确认退出后才转 `ObservedEmpty`。迟到旧会话观察可交历史记录，但不能修改活跃投影。成员增减事件缺序或溢出时标记投影不完整，下一次通过渠道公开快照对账；不能将缺事件当房间无人。

识别文本由context协调器先交memory.commit_input持久化，再安装投影；持久化失败返回明确错误，不向调用者确认完整observe成功。识别文本按 `input_ref` 与原输入关联，不以“当前最后一句”关联。原始音频在渠道/生成通路，本模块只保留具名引用和必要文本。没有成员身份时不能给未知用户开启对话窗口。

同一作用范围的修改不跨网络 await；投影自身无需持有渠道锁。上下文准备先 capture，再进行来源/检索 I/O；结果携原版本，由上层复查是否过时，不能为一致性锁住房间直到生成结束。

## 依赖与迁移

只依赖 `context.models`、`channels.models` 及通用时钟/类型，不依赖 memory/sources 的实现。模块协调器把历史摘要和来源新鲜度组合成 `SituationSnapshot`，不是让本投影读取全部存储。

当前 `voice_agent/observation.py` 主要是决策轨迹/测量辅助，**不是已实现的上述投影**；现有情境仍散在 `VoiceAgent`、串门控制器及渠道快照。首条切片先接渠道会话/连接事实并提供 snapshot，再迁成员和语音活动；不能按旧文件名整体移动。

## 验证与变化场景

迁入后在 `tests/context/` 覆盖：同 ID 重复、同连接乱序、重连重用序号、S1 识别迟到不覆盖 S2、缺成员事件标不完整、快照不可变。公开验证通过 `context.observe/snapshot`，内部排序算法可单测。

变化场景：平台增加事件字段时，渠道先转换公开观察；此处只在业务需要新情境事实时变化。替换历史存储不应改投影。当前仅完成设计推演，未运行目标接口测试。
