# 对话生成

当前状态：**供应商生成实现已迁入，任务式最终接口仍在推进**。`VoiceBackend`、能力查询与注入端口见 [公开 API](api.py)，不可变生成结果与 Live 回合证据见 [公开类型](models.py)，工厂见 [包入口](__init__.py)。级联、Gemini Live、代理 WebSocket、文本净化和模型观测由本模块拥有；旧 `voice_agent/backends/` 入口已删除。

本批已移除生成中的 `MemoryStore.append` / `ContextBridge.record_turn`：级联返回 `VoiceReply`，Live 通过 `set_generated_out` 交付 `GeneratedTurn`；应用核实渠道会话及播放结果后才提交上下文。控制候选保留行为定义的 `VoiceControlDecision`，不再重新编码成无类型 `raw` 字典。策略实例由装配注入 `ReplyPolicyPort`，生成不再导入行为工厂；共享上下文只通过 `MemoryTools` / `HistoryReader` 窄端口读取与请求授权工具。

下文完整任务、输出证据流和许可接口仍是**待实现契约**，不得视作当前工厂返回的兼容能力均已隔离。全局约束见 [系统架构](../../ARCHITECTURE.md)，目录规则见 [代码要求](../../docs/coding-standards.md#模块目录与接口导航)。

## 职责与阅读地图

提供“识别输入、解释控制候选、按已批准意图产生表达”的能力，隐藏 ASR、模型、TTS、供应商连接与可重建缓存。是否回应/执行控制归行为，播放归渠道，权威历史和记忆投递归上下文；不能通过供应商回调直达 SDK 或存储。

| 内部边界 | 独有知识/状态 | 设计入口与当前证据 |
| --- | --- | --- |
| 级联识别与表达 | ASR/分类协议、提示/分句/TTS、每任务队列与取消、可重建提示缓存 | [级联子模块](_internal/cascade/README.md)；`MimoCascadeBackend`、`AliyunDeepSeekBackend` |
| 实时模型会话 | 持续连接、协议事件、本地轮次、转录关联、输出许可、重连及任务回收 | [Live 子模块](_internal/live/README.md)；`GeminiLiveBackend`、`VoiceAgent._wire_live` |
| 公共输出与证据 | 单次流消费、音频编号、生成终态到输出源失败、文字与音频关联证据 | 本页下文；候选 `_internal/output.py`，无需单独建子模块 |
| 供应商适配 | Gemini/MiMo/阿里/DeepSeek 的鉴权、协议解析、网络资源及能力声明 | 当前实现在 `_internal/cascade/` 和 `_internal/live/` |

选择两个有独立生命周期的子模块，不把 ASR/LLM/TTS 每一步变成透传服务。识别与表达是可分别调用的同模块能力；预览和普通对话复用表达任务，不使用真实房间状态。供应商替换只影响适配及装配配置，共同契约不变时无需调用者学习供应商参数。

## 已实现调用与后续目标

当前应用用 `create_backend(settings, history_reader, reply_policy=policy)` 装配。设置经只读 `DialogueSettings` 结构协议读取，不反向依赖配置加载器；`bind_history` 更换后续级联请求读取源，Live 不读取本地历史。级联消费 `handle_utterance` / `StreamingUtterance.handle_utterance_stream`，关闭使用 `aclose`；同实例级联生成串行执行，等待可取消，独立实例可并行；Live 继续使用持续连接及独立输入/输出回调，不强制文字中转。`capabilities()` 已用于试听选择级联或 Live 能力；`LiveLimits` 配置候选输出字节预算和迟到转写窗口，连接器可注入用于外部协议测试。声音产出和 `GeneratedTurn.selected` 均不证明房间已播放。

以下为后续目标操作；已实现的过渡能力以 API 与代码 docstring 为准。

有 I/O 的单次操作为 async，流接口返回受控异步迭代器，能力/只读证据查询可同步。`api.py` 不导入内部实现，工厂在包入口装配；不返回原后端或共享可变设置。

| 操作 | 语义与失败 |
| --- | --- |
| `capabilities() -> DialogueCapabilities` | 声明识别、控制解释、生成、Live、固定台词表达等能力；能力缺失为 Unsupported，不靠 hasattr 探测。Live“按文字意图表达”不自动保证逐字朗读 |
| `recognize(input: AudioInput, permit: AttemptPermit) -> Recognition` | 自动级联须带行为在 ASR 前预留的合法尝试许可；Recognized/NoSpeech/RecognitionFailed 可区分，保留同一 attempt_id/input_ref/session/speaker。只识别，不接话、不写历史；现有试听走独立 preview 操作，不伪造自动许可 |
| `interpret_control(input: ControlInput) -> ControlInterpretation` | 输入已识别文字与引用；输出行为模块定义的 NoControl/LeaveCandidate/VolumeCandidate 或明确 Invalid/Unavailable。只解释语义，不生成回复、不执行控制 |
| `generate(intent: SpeechIntent, context: PreparedContext) -> AsyncIterator[GenerationEvent]` | 验证相同会话、上下文引用和能力；关联动作输出文字、音频、单一正常/失败终态。业务失败类型化；程序错误抛出，取消清理后传播 |
| `audio_output(events: AsyncIterator[GenerationEvent]) -> GeneratedAudio` | 公开的单消费者音频适配；`frames()` 提供渠道 OutputFrame，`evidence()` 返回只读表达证据，`aclose()` 级联关闭源。调用者不在 app 自写帧编排/终态转换 |
| `preview(request: PreviewRequest) -> PreviewResult` | 生成独立的短试听 WAV，返回 PreviewArtifact 或具名失败；不需要真实渠道会话、不接受伪造 SessionRef，不调用 channels.execute，具体预算见下文 |
| `open_live(request: LiveSessionRequest) -> LiveSession` | 受控会话具有 send_input/events/resolve_turn/resolve_control/snapshot/aclose；固定渠道及上下文来源绑定，不把 Live 硬转成逐句 generate；详细许可和事件约束见 Live 子模块 |

`ContextToolPort` 是 `dialogue.api` 中由工厂注入的窄端口：`recall(request: context.models.RecallRequest) -> RecallResult`、`remember(request: RememberRequest) -> RememberReceipt`。应用只把它适配为上下文公开能力，不向生成注入 ContextBridge、数据库或任意回调。请求的 `ContextRef(session_ref, source_generation, binding_id)` 来自 PreparedContext；remember 的 WriteAuthorization 引用由上下文 prepare 根据已观察输入和权限签发的 authorized_write_grants；生成仅选择已有合法 grant 引用，再由上下文验证，不能自己拼装授权字段。模型只能提供 query/content，不能伪造授权、选择 SourceBinding 或把“工具超时”说成已保存。外部写入待确认时不重放新 ID，沿原请求查询/重试由上下文负责。

## 公开数据与消费约束

| 类型 | 必要字段与不变量 |
| --- | --- |
| `DialogueCapabilities` | 可用能力的具名集合及格式/容量限制；固定表达和严格逐字合成分别声明 |
| `AudioInput` / `ControlInput` | input_ref、渠道 session_ref、speaker 已知/混合/未知；前者有已验证格式/有界 PCM，后者为已识别文字。自动级联的识别结果、ControlInput 与 SpeechIntent 沿用同一 attempt_id，不在分类或生成时再扣额度。保留解析结果，不传原 JSON |
| `GenerationEvent` | `generation_id`、`action_id`、`session_ref`、`event_seq`；具名分支 TextDelta/TextFinal/Audio/GenerationCompleted/GenerationFailed。终态后无数据，增量与最终文字不可混淆 |
| `OutputFrame` | 使用渠道定义：action_id、session_ref、output_id、frame_seq（每播放从 0 连续）、PCM16 与采样率/声道。output_id 是稳定流身份，可取 generation_id；单帧不超过渠道 64 KiB 且按完整采样对齐，供应商大块先切分再赋连续 frame_seq，不在应用外层重新编号 |
| `GenerationEvidence` | 生成状态、最后事件/帧编号、文字片段及可证明的帧关联；关联未知显式保留，不能用整段文本推定中断时已播放前缀 |
| `PreviewRequest` / `PreviewArtifact` | 请求含 preview_id、kind=voice/enter/leave、text、可选 voice 及已解析的只读模型配置/人格快照；产物含 preview_id、实际表达 text、WAV bytes、sample_rate、duration；没有 channel session/action/attempt，也没有写记忆授权 |
| `LiveSessionRequest` | 渠道 session_ref、PreparedContext/context_ref、能力和有限预算；来源变化重建，不热改旧会话绑定 |

声音和伴随文字是不同渠道动作。SpeechIntent 首批声明一个主输出；兼容 `reply_text_to_channel` 时，文字 SendText 使用独立 action_id 并以 parent_action_id 关联主意图，音频帧绑定 Play 的 action_id，分别记终态。生成证据可复用同一段表达，但文字成功不代表音频成功，不用一份渠道结果伪聚合两个副作用。

`GeneratedAudio` 是一个消费适配而非第二个播放器：只有 `frames()` 消费上游 events，`evidence()` 不消费、不缓存全量 PCM，禁止同时再直接迭代原 events。正常终态后 frames 自然结束；失败终态、缺少正常终态或中途异常转换为渠道公开 `OutputSourceError`。渠道关闭 frames 时调用 aclose，关闭源生成器及供应商任务；失败不能转换为普通 EOS 让播放误报成功。渠道关闭/提前拒绝后 `evidence()` 仍须立即返回已观察部分及 cancelled/unknown 标记；不能等待无人继续消费的正常终态，也不把未观察的文字补成完整表达。若直接消费纯文字流，同样必须验证正常终态；记录草稿与提交已发送文字分开。

Live 只允许一个控制事件消费者；获批轮次通过输出引用取得同一 GeneratedAudio 契约。它的 aclose 只撤销所属轮次；供应商无法安全取消/识别轮次边界时关闭旧模型会话并重建，不伤及新渠道会话。Live 的退房/音量工具通过 ControlProposed → 行为授权 → 渠道执行 → resolve_control 回传类型化结果，不作为记忆工具或普通发言处理。状态机、预算和晚转录语义以 Live 子模块为准。

## 独立试听能力

现有 `src/voice_agent/preview.py` 的 `PreviewService.generate/_generate` 使用临时后端和 EphemeralMemory，不进房；级联做 TTS 或公告改写，Live 创建临时模型会话并收集音频，返回浏览器 WAV。因此目标 `preview` 是独立公开操作，不要求调用方构造 SpeechIntent、真实 SessionRef 或普通 PreparedContext。

PreviewRequest 在入口校验 kind、文本 1–500 字、音色名至多 80 字；人格来自已解析配置快照，无需访问真实房间记忆。工厂按现有配置选择已声明试听能力的供应商，不让用户提交任意连接参数。能力不可用、Busy、超时、无音频、格式变化及模型失败分别返回 PreviewFailure；PreviewArtifact 的 WAV bytes 仅在 HTTP 适配边界编码成现有 wav_base64 响应。

内部复用 ExpressionJob 的表达/分句/TTS与供应商适配，但目标是私有 `PreviewTarget(preview_id)`，房间表达则是 `RoomTarget(action_id, session_ref, output_id)`。job 的内部 ExpressionEvent 不强带渠道帧；房间投影才形成 GenerationEvent/OutputFrame，试听投影收集短 WAV，不能把试听 PCM 填入虚构的渠道 OutputFrame。Gemini 试听同样用独立临时模型连接，无采集、无工具端口/授权，不占用活跃 LiveSession，不消费行为额度。

沿用现有预算：同一服务仅一个试听任务，忙时明确拒绝；生成至多 30 秒，单声道 PCM16 总时长至多 20 秒，采样率 8–96 kHz 且整段固定，累计字节上限按采样率推导。取消、失败和超时均关闭临时任务/连接，现有收尾上限 3 秒；不能把浏览器取消转成继续生成的无人观察任务。试听不写 context 历史/outbox，不触发 behavior.feedback；最终没有可用音频则失败，不伪造空 WAV 成功。预算与清理需迁移测试覆盖，本文不是新实现已验证的保证。

## 内部静态依赖与装配

```text
包工厂 → 级联或Live实现、所需供应商适配、公共输出适配
级联实现 → 私有供应商窄端口、私有任务类型
Live实现 → 私有供应商窄端口、本地轮次/闸门
公共输出适配 → 公开生成类型、channels.models输出源/帧类型
供应商适配 → 私有端口/公开类型、网络和音频技术库
```

实现不反向依赖工厂；适配不导入级联/Live控制器；级联和Live不相互调用。需要的私有端口候选放 `_internal/ports.py`，不建通用插件注册框架。公共静态依赖只用 `behavior.models`、`context.models`、`channels.models` 和 core 技术机制；运行时 ContextToolPort 注入不等于实现互导。可选依赖在对应能力入口加载，缺失明确返回能力不可用，纯文字桥接启动不建立供应商连接。

## 状态归属与只读接口演练

级联 job 拥有本任务 sender/队列/LLM/TTS 资源；Live 会话拥有连接、重连、reader/writer/dispatcher/工具任务；输出适配只拥有源消费和小量证据。缓存可重建，不写权威历史。渠道拥有播放清理；应用拥有跨模块流程和关闭顺序，不能拿模型私有锁。

自动级联伪调用：AudioReady → behavior.decide 得 ProcessInput(AttemptPermit) → recognize(input, permit) → 同 attempt 的识别/分类候选交行为 → NoAction 结束、控制命令或 SpeechIntent → prepare → generate → audio_output → 渠道 execute(Play, frames) → 读取 evidence 与真实 outcome → context 记录、behavior 反馈。AttemptPermit 含 attempt_id/input_ref/session_ref/policy_version/expires_at；空 ASR、最终沉默、控制分支及失败也消耗已预留尝试，不因未 Speak 退款。所有已开始尝试最终送 AttemptFinished(attempt_id, handled_at, end_reason) 完成冷却结算；执行终态另送 ExecutionFinished，不重复设置冷却。若生成前准备失败，发送 NotExecuted；若已有执行部分输出，则按渠道 ActionOutcome 反馈，不伪造完成。Live 使用自身许可端口，Hold 是明确等待信息，不把全局 NoAction 延后重试。Allow 被明确拒绝且确认没有绑定该 action 时，应用送 NotExecuted(stage=permission) 释放待执行占用；重复 Allow 返回原绑定而不是失败，不能把已获批动作误终结。许可响应超时/结果未知先查 snapshot 中绑定状态，不能当未执行后生成新 action。

## 验证与迁移入口

现状证据：权威历史写入已迁出生成，模型协议和资源已归本模块；`VoiceBackend` 仍接收兼容设置和上下文端口，MiMo 的 `_decide_utterance` 仍编排策略查询，阿里流仍用每后端实例的音频 sink，Gemini 仍协调策略许可与模型事件。独立 ExpressionJob、版本化 PreparedContext、Live 提议/许可和单消费者 GenerationEvent 仍未落地，不能把源码归位当作全部契约完成。

先按 [准备计划](../../docs/superpowers/plans/2026-11-08-module-refactor-readiness.md) 让房间/播放拥有者收敛，再迁移级联 job；Live 在有界许可/关闭隔离试验通过后替换。共同生成行为测试复用到多个后端，Live/控制解释等特有能力单独测。现有证据入口为 `tests/test_aliyun_deepseek.py`、`tests/test_backend_audit_regressions.py`、`tests/test_live_turn_lifecycle.py`、`tests/test_gemini_session_lifecycle.py` 和各子模块列出的回归测试。

目标检查包括：两任务不串状态、分类失败不当 NoControl、未授权音频零泄漏、慢消费者/超时有界、切房迟到帧失效、重复许可不重放、断连清掉 ready、工具关闭不写新连接、流取消触达供应商清理、部分生成不写完整历史。新测试落 `tests/dialogue/`；工厂迁移由供应商回归及应用集成测试验证；[来源/历史绑定测试](../../tests/dialogue/test_context_binding.py) 使用真实来源桥、历史文件及本地模型服务验证迟到工具不能写入新来源、刷新后读取新历史，[流式协议测试](../../tests/test_aliyun_deepseek.py) 验证同实例并发请求不会把输出送入另一请求的 sink。[生成日志测试](../../tests/dialogue/test_generation_logging.py) 经供应商连接边界验证正文/会话句柄不进入日志。生成请求捕获来源代次，在网络和工具等待后复查；旧供应商回归仍有私有方法/属性断言，属于后续测试维护项，不代表完整目标契约已覆盖。真实模型/房间验收由应用级双机器人验收另行记录。


## 当前失败与公告证据

`RecognizedGenerationFailed` 保留已完成的用户识别事实，排除助手草稿；应用将该输入交上下文记录失败回合，生成模块不恢复直接写历史。Live 公告通过公开 `GeneratedTurn(internal=True)` 的文字及生成终态交付，应用不读取供应商的私有转写缓存；实际播放结果仍由渠道 `Playback` 确认。相关入口以 `dialogue.models`、`LiveDialogue.set_generated_out` 为准。


级联 `VoiceBackend.handle_utterance` 与流式 `StreamingUtterance` 的可选 `on_recognition` 在最终识别完成时同步交付本次输入，取消不撤销已交付事实。应用将取消后的输入交给已绑定的上下文记录端口；生成仍不直接追加权威历史。此回调仅属于本次调用，不使用共享的最后转写字段。自动合成的任务及音频 sink 固定所属调用，独立 `tts(text)` 不复用自动任务或其回调。

应用对非流式生成等待和播放取消同样收尾已识别输入；Live 内部公告在渠道确认播放成功后统一提交助手文本，不制造用户记录。`tests/test_voice_history_outcomes.py` 通过真实 JSONL、模型阶段取消及 Live 消息验证这些行为。
