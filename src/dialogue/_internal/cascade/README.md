# 级联识别与表达

公开入口见 [对话生成](../../README.md) 与 [api.py](../../api.py)。本子模块隐藏 ASR 协议、聊天提示/净化、分句和 TTS，不判断概率、不执行控制、不写权威历史。

## 接口与状态归属

`recognize` 只识别已经行为准入的 `AudioInput`。检查许可会话、attempt_id及可用的输入引用/期限；同尝试保存进行中任务或结果，异载荷冲突。最多保留1024条，满时先回收已结束条目，所有进行中时拒绝新请求。空识别与供应商失败具名返回，取消清理后传播。

`open_recognition` 是阿里后端的流式能力，空音频头绑定原许可/会话/说话者和固定采样率；
[识别流](recognition.py) 拥有 WS、64帧队列、句ID修正和绝对期限。临时转录可查询但不能
冒充最终结果；缺少最终转录/正常终态、格式非法、队列溢出均失败关闭。同许可只接受一个
流式拥有者，重复打开拒绝；最多8流，`aclose` 关闭上传和reader，后端关闭也回收所有流。

`interpret_control` 使用独立语义请求，保留 `VoiceControlDecision`。非法JSON、严格布尔失败或互斥控制返回invalid，网络等失败返回unavailable；不会把失败当作NoControl。应用再交行为授权，不能由供应商执行动作。

`generate` 要求意图与准备上下文会话、版本一致。每个任务创建独立供应商实例、设置快照、分句队列及音频sink，避免共享后端字段被另一请求覆盖；任务结束关闭自己的HTTP/WS资源。普通回复读取本次 PreparedContext；固定台词不调用聊天，公告不读房间历史。是否启动表达由行为决定。

## 供应商适配

- [MiMo](mimo_cascade.py)：ASR、严格控制解释、聊天净化及PCM合成；整段产物仍由公共任务按64KiB切分，任务期限与容量限制由公共输出拥有。
- [阿里/DeepSeek](aliyun_deepseek.py)：双向流式ASR WebSocket、DeepSeek SSE分句及阿里双向TTS。
  DeepSeek按任务配置选择none/low/high/max（默认low），生成任务固定原档位，只消费正式回答content；reasoning_content不复制到语音/历史，
  仅工具协议必要时在本任务内透传供应商要求的推理字段，不持久化或广播。推理标签正文和
  缺少正式回答失败关闭；不能以关键词清洗推理字段代替结构隔离。每任务文本队列16项；TTS sender与reader共同收尾，音频采样对齐、时长至多60秒；在聊天结束前可以产出音频，不能失败后自动重播已发前缀。
- [公共任务/输出](../output.py)：单订阅、有界背压、序号、终态、关闭与渠道源失败转换。

旧复合 `handle_utterance`、`_decide_utterance`、合并“控制与聊天”的优化和 `handle_utterance_stream` 已移除。控制解释、行为选择和表达分别产生可供下一层直接使用的事实，不能借兼容入口绕过许可。

## 验证

真实本地协议回归见 [阿里协议测试](../../../../tests/test_aliyun_deepseek.py)、[MiMo任务与历史](../../../../tests/test_mimo_chat_history.py)、[控制解释](../../../../tests/dialogue/test_control_interpretation.py)。它们保留供应商reader、分句、任务与HTTP/WS真实代码，替换外部服务器，不替换被验证的生成协调。

新模型只要保持公开事件、错误、取消和产物契约，应用流程无需学习其分片格式、JSON或重试细节。供应商能力改变时显式修改capabilities，不通过私有属性探测。

阿里/DeepSeek 的 `assess_reply` 使用独立配置快照与HTTP会话，JSON模式、关闭思考、最多128输出token、总请求4秒；近期10条上下文为观察资料，重复/未知字段、截断/空白均返回invalid，网络失败返回unavailable。候选只供行为决策，不进入语音。正式生成继续复制用户配置的思考档位。

表达任务通过有界timing事件及安全日志报告阶段耗时：`llm_start/llm_headers/llm_first_token/llm_first_sentence/llm_total` 自模型请求开始累计，首字只计正式content；`tts_start/tts_ready/tts_first_audio/tts_total` 自TTS启动累计。图像处理与模型思考包含在首字等待中，供应商未提供二者独立计时，不能声称已拆开。生成、取消和失败日志保留原关联ID，不记录正文或推理；已产生的阶段证据在失败时仍可查询。
