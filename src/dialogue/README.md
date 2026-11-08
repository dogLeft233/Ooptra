# 对话生成

本模块提供输入识别、控制候选解释、已批准意图的表达任务，以及持续 Live 会话。公开契约在 [api.py](api.py)，不可变输入、事件和证据在 [models.py](models.py)，工厂在 [包入口](__init__.py)。生成不执行渠道动作、不决定概率或额度、不提交权威历史。全局状态归属见 [系统架构](../../ARCHITECTURE.md)。

旧 `handle_utterance` / `handle_utterance_stream` 复合入口、模型内概率/窗口判断、直接音频及控制回调已删除。应用通过识别事实交行为决策，再持已批准意图和准备上下文启动生成；Live 通过提议/许可协议协作，不强制文字中转。

## 能力与导航

| 能力 | 公开入口与使用约束 | 实现导航 |
| --- | --- | --- |
| 级联识别 | `VoiceBackend.recognize` 接收有界 `AudioInput` 与原尝试许可；同尝试重复复用识别结果，异载荷冲突，不调用聊天 | [级联识别与表达](_internal/cascade/README.md) |
| 流式识别 | `open_recognition` 以空音频头和许可建立 `RecognitionStream`，有界 `feed` / `finish_input` / `result`；临时转录只用于可替换投影，只有最终文本可交决策 | [级联识别与表达](_internal/cascade/README.md) |
| 控制解释 | `interpret_control` 返回解析后的候选或 `invalid` / `unavailable`；非法模型响应不能当作 NoControl | [级联识别与表达](_internal/cascade/README.md) |
| 房间表达 | `generate` 返回独占 `ExpressionJob`；固定台词、公告和普通回复显式区分，绑定 `SpeechIntent` 与 `PreparedContext` 的会话/版本 | [级联识别与表达](_internal/cascade/README.md) |
| 渠道输出适配 | `audio_output(job)` 的 `GeneratedAudio.frames()` 单订阅产生渠道帧；`evidence()` 不消费源、不缓存全量PCM、不代表已播放 | [_internal/output.py](_internal/output.py) |
| 持续 Live | `open_live` 固定渠道/来源绑定，`LiveSession.events()` 单订阅提议、输出和控制事件；许可与实际执行分别确认 | [实时模型会话](_internal/live/README.md) |
| 独立试听 | `create_preview_service().preview` 返回 WAV 产物或具名失败，无真实房间、行为尝试或记忆授权 | [_internal/preview.py](_internal/preview.py) |
| 音色管理 | `create_voice_library()` 的 `VoiceLibrary` 创建、分页查询与就绪校验；云端拥有目录，当前音色选择仍由维护配置拥有 | [_internal/voice_library.py](_internal/voice_library.py) |
| 模型自检 | 临时后端 `probe` 返回分阶段证据；不对活跃房间后端调用，调用方负责关闭 | [_internal/diagnostics.py](_internal/diagnostics.py) |

MiMo、阿里 ASR/DeepSeek/阿里 TTS、Gemini Live 为实际适配。OpenAI Realtime 尚未实现，选择它会明确失败；不会用 Gemini 协议伪装支持。`capabilities` 声明 Live、流式表达和固定合成差异，不靠供应商名猜测能力。

## 任务、流和取消

`ExpressionJob` 独占供应商任务及资源，状态为 running → completed / failed / cancelled。事件含稳定 generation/action/session、连续 event_seq，区分文字增量、最终文字、音频和单一终态。应用不逐供应商解析、不为生成流重新编号。

任务队列默认16项，供应商音频先按完整 PCM16 采样切为每项至多64KiB，再进入有界队列。慢消费者形成背压，绝对单调时钟截止同时约束生产和等待；不是不断重设的滑动超时。关闭/取消等待本任务资源回收，不关闭其他任务。`Recognition.elapsed_ms`、`ControlInterpretation.elapsed_ms` 和 `GenerationEvidence.timings_ms` 交付安全耗时事实；`generation_total` 包含本任务背压等待，串行MiMo额外报告chat/tts，重叠流不伪造可相加的独立阶段。程序错误和供应商失败的对外证据均不含原始响应或凭据。

`GeneratedAudio` 是消费适配，不是播放器。正常生成终态才是普通 EOS；失败、缺少正常终态或异常转换为渠道 `OutputSourceError`。输出帧使用 job generation_id 作为 output_id，从0连续编号。应用把整个流交渠道，再联合生成证据和真实播放结果提交上下文，不能把产出音频当作扬声器播放完成。

渠道提前拒绝、尚未读取第一帧就关闭流，也会取消对应生成。部分消费后 `evidence()` 立即返回已观察文字/帧及取消或失败状态，不等待无人继续消费的终态。未证明文字与音频片段映射时，不把整段草稿推断成已播放前缀。

## 上下文和工具权限

生成开始捕获不可变 `PreparedContext` 的人格、历史、版本和来源。级联每个任务有独立供应商实例/请求状态，后续刷新不改当前任务快照。工厂不接收全局历史或来源桥；房间主路径只使用本次准备上下文。

类型化 `ContextToolPort` 由应用注入 `ContextService.tools(prepared)`，不把数据库、来源桥或可变 outbox 暴露给模型。模型只提供 query/content，适配器将其转换为 `RecallRequest` / `RememberRequest`；供应商真实 tool call ID 作为幂等身份传入，不能由模型重定向来源或伪造写入授权。绑定工具在上下文拥有者处复核原会话、来源和合法 grant。queued/unknown 不是外部已保存。

## 独立试听和诊断

`PreviewRequest` 只含 preview_id、kind、文本、音色和人格，没有虚构的 SessionRef/action/attempt。单服务只允许一个试听；文本1–500字，音色至多80字，生成至多30秒，音频为固定8–96kHz单声道PCM16、至多20秒。无音频、超预算或采样率变化失败，不返回空 WAV 成功。资源收尾上限3秒；浏览器取消传播并关闭临时连接。

级联试听复用提示/公告净化/TTS适配；Live 试听使用独立连接的有界收集器，不连接渠道、不启用控制/记忆工具、不借用活跃 LiveSession。HTTP 的 base64 编码只在 `generate` 响应适配出口进行。

自检 WAV 入口先验证完整PCM、10秒与2MiB预算。级联分别观察 ASR、控制分类和 TTS；Live 输入转写与输出自检使用两个独立连接，原输入轮次的迟到音频不能证明输出自检成功。Live 工具只记录候选，不执行退房等真实动作。取消关闭全部临时资源。

## 验证与变化范围

- [表达任务测试](../../tests/dialogue/test_expression_jobs.py)：单订阅、64KiB切分、有界取消、源失败，以及首帧之前关闭仍回收供应商。
- [Live 许可测试](../../tests/dialogue/test_live_permissions.py)：未许可零输出、重复/旧版许可、Hold后晚转录放行、客户端中断保留连接、队列溢出与期限失败。
- [控制解释测试](../../tests/dialogue/test_control_interpretation.py)：真实HTTP JSON模式、合法/非法候选、外部失败与响应隐私。
- [阿里协议测试](../../tests/test_aliyun_deepseek.py)：真实本地HTTP/WS上的ASR重采样、SSE分句、音频先于LLM完成、并行任务隔离、取消和半流失败。
- [生成历史测试](../../tests/test_mimo_chat_history.py)：固定上下文、当前问题一次、历史净化、推理泄漏零TTS、草稿不入权威历史。
- [来源绑定](../../tests/dialogue/test_context_binding.py)、[日志隐私](../../tests/dialogue/test_generation_logging.py)、[独立自检](../../tests/test_voice_audio_diagnostics.py)及[试听](../../tests/test_voice_tools.py)验证对应边界。

更换共同能力不变的模型供应商，应主要修改此模块适配与装配配置；调整回复概率、冷却或抢话规则应修改行为模块。真实供应商和 Oopz 时序仍由应用的[双机器人验收](../../docs/bot-pair-acceptance.md)另行证明，协议夹具通过不替代真实模型验收。


音色管理只支持当前 Qwen Audio TTS 级联后端，沿用语音 WS 域名与 DashScope 凭据。
每个请求独占最长 45 秒的 HTTP 会话，取消关闭连接；创建无自动重试，超时或取消不代表云端未创建，需先刷新确认。
上传样本只在请求内存中使用，不新增本地音频目录。WAV 在本地校验格式、采样率、时长和完整性，MP3/M4A 的声学规格由供应商检查。
创建返回 ID 不代表就绪；只有 `OK` 且模型匹配才允许界面使用。控制台选择经现有配置 CAS、持久化与热应用回执执行，下一次新生成语音使用新配置。
协议与输入验证见 [音色管理测试](../../tests/dialogue/test_voice_library.py)，保存与运行应用见 [选择配置测试](../../tests/integration/test_selected_voice_configuration.py)。
