# 级联识别与表达

状态：**供应商实现已迁入，任务式契约待实现**。当前入口为 [MiMo](mimo_cascade.py) 与 [阿里/DeepSeek](aliyun_deepseek.py)，由模块工厂装配。生成读取已确认历史、返回不可变结果，不再提前追加用户/助手历史；应用根据播放结果提交。以下任务式接口仍是目标设计。本页定义级联内部边界；公开契约及依赖以 [对话生成](../../README.md) 为准，实际播放、历史提交与额度不在这里。

## 为什么保留为一个内部子模块

选择“独立识别能力 + 任务式表达”，不按 ASR → LLM → TTS 的步骤各建服务层。ASR、文本分句和 TTS 协议是独立替换的叶适配，但流式表达需要统一拥有取消、终态和资源。把每一步拆成多层对象，会把分句、异常后是否重试和清理顺序暴露给上层；继续保留一个 `handle_utterance` 则无法表达“听懂但沉默”。

内部候选位置为 `_internal/cascade/session.py`、`_internal/cascade/models.py`；供应商编解码放模块内 `_internal/adapters/` 的叶文件。它们是迁移时的候选文件名，本次不创建空 Python 文件。

## 内部接口与类型

以下均为设计签名；接口落地后以代码/docstring 为准，不保留第二份签名。

| 操作 | 输入与输出 | 隐藏的知识 |
| --- | --- | --- |
| `recognize(AudioInput, AttemptPermit) -> Recognition` | 自动级联带行为预留许可、完整输入 ID、渠道会话、音频及格式；返回带原 attempt_id 的 Recognized/NoSpeech/RecognitionFailed 联合 | 重采样、ASR 协议、空识别/净化、供应商失败；不调用聊天或写历史 |
| `interpret_control(ControlInput) -> ControlInterpretation` | 已识别文字、input_ref、speaker；返回行为模块定义的候选或明确失败 | 结构化模型结果解析；不得把非法响应当作 NoControl，不执行命令 |
| `start(ExpressionRequest) -> ExpressionJob` | RoomExpression 含已批准意图/PreparedContext；PreviewExpression 含已解析试听文字/人格配置，两者为具名互斥分支；返回独占生成任务 | 提示组织、固定台词/自由表达、LLM/TTS 协调 |
| `ExpressionJob.events() -> AsyncIterator[ExpressionEvent]` | 单消费者私有事件；顺序输出具名文字、原始有界音频块、正常/失败终态；房间/试听各有公开投影 | 分句、队列、任务关联、停止生产与异常观测 |
| `ExpressionJob.aclose() -> None` | 幂等；停止本任务，不停止其他预览/生成任务 | 取消 sender/TTS/LLM，等待收尾，归还或关闭连接 |

`ExpressionRequest` 是 RoomExpression 与 PreviewExpression 联合。RoomExpression 固定自动级联的 `attempt_id`（人工表达为具名非自动分支）、`generation_id`、`action_id`、`session_ref`、`context_ref`、`SpeechIntent`、`PreparedContext` 和输出格式；PreviewExpression 只有 preview_id、已解析试听请求/人格配置及输出预算，不接受伪造真实会话。两者共用私有 ExpressionEvent，房间投影才产生公开 GenerationEvent/渠道帧，试听投影产生有界 WAV 产物；完整试听契约见 [模块公开预览能力](../../README.md#独立试听能力)。`AudioInput` 的输入引用/会话/格式从渠道公开类型组合；不得接受用户给出的任意路径、SDK 句柄或原始 dict。音频长度、PCM 对齐、采样率和容量在构造时检查；合法对象后续直接消费。自动级联先由 AudioReady 取得 ProcessInput(AttemptPermit)，其 attempt_id/input_ref/session_ref/policy_version/expires_at 必须对应输入；分类、生成沿同一 attempt，不在这里重复判断/扣减额度。同 attempt 的重复识别请求复用进行中的任务或原结果，不开启第二次 ASR；同许可配不同输入返回冲突，不把重复处理藏在有限额度背后。空识别、控制、沉默与失败仍消耗该次尝试，应用最终发送 AttemptFinished 完成冷却；人工表达与预览不伪造自动许可。

`ExpressionJob` 持有本轮队列和任务；HTTP session 可以由供应商适配池拥有，但 `_audio_sink`、文本队列和当前任务不得放在共享后端实例字段中被预览/另一回合覆盖。模型连接和提示缓存是可丢弃缓存，不是历史库。`PreparedContext` 中的人格、历史和来源只读，任务开始后不重新从全局设置或桥查询。

## 状态与流语义

```text
created → running → completed
                 → failed
                 → cancelling → closed
created → closed
```

私有 job 只有一个正常/失败终态；房间投影的生成流恰有一个 `GenerationCompleted` 或 `GenerationFailed`，试听投影在对应终态返回产物/具名失败；终态后不能再输出。外部取消/`aclose` 不强行往无人消费的队列写终态，任务记录可查询取消状态，`CancelledError` 在完成清理后传播。迭代没有正常终态就结束属于输出源故障，不是成功。

每轮内 `event_seq` 单调，文字片段区分增量与最终文本，避免接收方重复拼接。音频帧编号、播放确认范围由渠道公开类型定义；生成只在生产时赋单调编号，不把“产出”视为“已发送”。供应商只给整段音频时可在内部接受有界整段，但对外必须先按完整 PCM 采样切成每帧至多渠道 64 KiB，再赋连续 frame_seq；不能把整段直接作为一帧交给渠道。超过累计任务容量明确失败，不用“兼容”理由无限缓存。

文本分句为生成内部算法，必须处理跨 SSE chunk 的标点、工具响应和思考标记。只有净化后的表达文本进入 TTS；控制 JSON、工具请求与未经验证的联合响应不可以先播放后修正。候选控制与表达已拆开，迁移后不保留“普通回复失败后才检查退房”这种调用隐含顺序。

## 背压、取消与失败

- LLM → TTS 文本队列初始设计上限为 32 段且累计 64 KiB；生成音频待交付累计上限为 4 MiB，容量按字节计，不只看块数；它不是单帧限额，单帧仍受渠道 64 KiB 约束。值属于待验证内部预算，不新增用户配置项；首次适配用长段、慢消费者核对后可调整，始终有有限上限。
- 可暂停的生成任务在队列满时背压，等待受本轮期限约束；TTS 或消费者已失败时立即取消其他生产者，不等待永远不会被取走的结束哨兵。真实网络 reader 如不能安全暂停，容量耗尽明确失败并关闭供应商流。
- 故障包括识别/分类不可用、上下文不匹配、模型失败、合成失败、输出容量耗尽和超时，保留阶段及关联 ID；不给外部返回含密钥的原始供应商报文。
- 未产生输出前的读取重试也必须有总时限；一旦已经输出帧，不从头重试整轮。是否重新行动交行为决策，不能重复播前缀。
- 渠道停止消费会关闭受控输出流；关闭必须触达 `ExpressionJob.aclose`。任务关闭不因正在持有的队列锁/结束哨兵而阻塞。公开输出适配负责把失败终态/异常结束转换为渠道输出源失败，应用不自行实现这一算法。

## 上下文工具与数据归属

生成通过工厂注入的 `ContextToolPort` 执行 recall/remember，协议定义见 [模块公开契约](../../README.md)。LLM 只能给查询或内容；context_ref、授权 grant 引用和目标由已准备上下文及合法请求给定，不能让模型提供任意来源。工具结果可继续用于本轮提示，但不是“外部记忆已成功”的空泛承诺。

已准备上下文和工具结果是证据输入，助手全文仍为生成草稿。任务不调用 `MemoryStore.append`、`ContextBridge.record_turn` 或更改 outbox。实际发送/播放范围由应用将渠道结果与生成证据提交上下文。当前兼容桥接调用捕获创建时的来源代次，外部请求完成及每次工具调用前后复查；来源切换会拒绝旧生成，不能把旧模型的工具请求投递到新来源。

## 现状证据与迁移切片

- `MimoCascadeBackend._decide_utterance` 目前编排 ASR、注入的概率/窗口策略、控制判断、聊天与 TTS；`chat_with_intent` 把控制候选与回复合并。须保留外部行为，先让识别/分类可独立使用，再迁移策略与历史，不能把搬文件当完成。
- `AliyunDeepSeekBackend.handle_utterance_stream/_queue_text/_clear_stream` 目前通过每实例 `_audio_sink/_texts/_tts_task` 协调流，生成互斥覆盖 sink 绑定、生成与清理，同实例并发调用串行而不串音；首个表达任务切片将这些状态收进 job，保留 DeepSeek 分句与阿里 TTS 的流式能力。
- `VoiceBackend.effective_persona` 当前读取 context bridge；迁入后使用传入的 PreparedContext，不再追踪全局可变桥对象。

可复查测试：`tests/test_aliyun_deepseek.py`、`tests/test_mimo_combined_intent.py`、`tests/test_mimo_decision_recovery.py`、`tests/test_mimo_chat_history.py`、`tests/test_backend_audit_regressions.py`。新模块测试目标目录为 `tests/dialogue/`，不预建空测试。

首个切片：抽出阿里级联的独立表达 job，用真实分句/队列协调与模拟外部 WebSocket 验证；旧接话路径通过兼容适配调用，不立即重写所有后端。退出需证明两个并行 job 状态不串、NoControl 与分类失败可区分、固定台词不读历史、取消不留 sender/TTS、半轮失败不重播。权威记忆写入已迁出；随后逐项拆开 `_decide_utterance` 的策略流程；在此之前明确标为兼容过渡。
