# 音频输入与会话输出

状态：房间 `push_audio` 屏障及音频 pump / VAD / 静音音量控制已有真实实现，分别见 [会话服务](../session/service.py)、当前目录的 `duplex.py` / `vad.py` / `controls.py`，公开能力通过 [渠道接口](../../api.py) 与包工厂使用。队列和回调带采集代次，关闭清理旧帧；相对调整在锁内读取最新音量。完整播放任务/帧台账/统一输入事件尚未实现，下文对应部分继续为目标设计。属于 [渠道交互](../../README.md)。

## 边界与内部组织

输入和输出共享物理传输及关闭边界，先在同一内部子模块组织为输入 pump 与输出执行器；二者不是连续透传的 service/repository 层。输入拥有每位说话者的 VAD/采集窗口，输出拥有当前播放、音量与静音。若将来独立维护再拆子目录，当前不增加空文件。

| 状态/数据 | 拥有者及约束 |
| --- | --- |
| PCM 采集队列、所属事件循环、输入序号、VAD 窗口 | 输入 pump；回调带 binding/session 身份，关闭后丢弃；输入是 PCM16 little-endian、有效正采样率/声道和完整采样单元 |
| `PlaybackId(action_id, session_ref)`、播放状态、流游标、当前任务 | 输出执行器；每个会话最多一个活动播放器；不以“最后一次生成”作为播放拥有者 |
| `next_frame_seq`、已提交帧/采样数、可确认播放范围 | 输出执行器；同一播放内 frame_seq 连续、格式固定；重复/缺口/格式变化显式失败，不猜测重复数据可重播 |
| 实际已应用的 volume/mute 及版本 | 输出执行器；整数范围 0–100 且拒绝 bool；静音与保存音量分开。配置持久化归维护，渠道只报告应用结果 |

公开 `OutputFrame` 至少含 `(action_id, session_ref, output_id, frame_seq, pcm16, sample_rate, channels)`；frame_seq 从零开始，只对本次播放有序，不充当跨轮全局序号。构造器解析并保存合法格式，后续传输不再消费原始字典。空 PCM 不表示结束，正常 EOS 才触发 finish；首批只接受单声道 PCM16，其他能力需明确适配或返回不支持。新编码须先扩公开格式类型与能力测试。

## 内部接口与流契约

- `AudioOutput.play(request: PlaybackRequest, frames: AsyncIterator[OutputFrame]) -> PlaybackResult`：请求携带已登记动作、目标会话、截止时间；由渠道执行器调用。接管流关闭；正常结束后发 finish 并等待实际播放确认；已可能出声的失败不重试整流。
- `AudioOutput.retire(session, reason, deadline) -> RetireResult`：满足会话声明的 PlaybackRetirer；阻止后续帧、取消 reader、stop 当前绑定并等待 in-flight push 收尾，返回是否可安全复用传输。不能等待整个房间转换。
- `AudioOutput.apply(control: SetVolume | Mute | Unmute, session) -> OutputApplyResult`：应用实际音量/静音并报告独立的房间状态广播结果；广播失败不谎称音频增益未应用。此结果交维护记录，不能在此导入 config_editor。
- `AudioInput.attach(binding, session)`、`detach(session, deadline)`、`events()`：接到会话允许的输入绑定；只产归一化音频/活动观察；输入事件最终由渠道单一事件出口分发，不向多个消费者暴露同一 queue。
- `AudioTransport.push(frame, volume, deadline) -> PushConfirmation`、`finish/playback_status/stop`：内部适配端口，结果带 binding 身份和可核实状态；不向模块外返回浏览器对象。

正常 EOS 表示源正常完成。生成模块失败/缺少正常终态由生成模块公开输出适配抛渠道公开 `OutputSourceError`，输出执行器转换成 failed/partial；不能捕获后当 EOS 继续 finish。任意异常和提前拒绝均 `aclose` 源；调用者取消保留可查询收尾任务，记录已知范围后传播取消。

`PlaybackResult` 包括动作/会话、终态、失败阶段、已提交帧及采样数、已确认播放范围、最后确认时间与未知范围。通道仅能确认浏览器播放结束或已提交，不保证人耳听到。中断时若只有“帧被接收”的证据，不能将对应完整文字写成已播放历史；文本/帧关联由生成模块提供，渠道不臆测转录。主音频和补充文字发送是不同真实命令与执行 ID，生成证据可关联同一意图，但其完成结果不能互相替代。

## 状态机、背压及资源

```text
Pending → Playing → Draining → Completed
Pending → Rejected / Expired / Cancelled
Playing / Draining → Stopping → Cancelled / Failed / Partial
```

是否 Partial 根据已知副作用确定；已送入浏览器但播到哪里未知时保留“不确定输出”证据，不能回到 Pending 后重放。Completed 要求源正常终态且 finish/实际播放查询成功；模型 turnComplete 不能代替 Draining。

输入延续现有 `VoiceDuplex` 的 owner-loop 交接和容量 200 的帧队列作为首批兼容基线；只在所属 loop `put_nowait`，满载丢当前输入帧并累计丢帧/断点观察，不把缺口拼成完整一句。队列同时按字节限制：首批 4 MiB，单帧最大 64 KiB；越界拒绝并记一次限流诊断。输入处理不直接 await 识别/生成，避免模型慢导致无界排队；发生缺口时重置相关 VAD 窗口，不把不连续音频拼成完整输入。监听白名单 `listen_only_uids` 是渠道采集授权，在音频进入 ASR/Live 前过滤；与行为中的回应对象偏好不同。VAD/输入活动可被行为观察，接话概率与关键词判断不藏在 PCM 转换中。

输出使用 pull 流背压，不再额外囤积整轮音频；最多一帧在推送中，最大单帧 64 KiB，超大帧在生成适配按完整采样切分后提供。网络 push 和等源下一帧均受播放总截止/空闲截止限制；同会话第二个播放器返回 Busy，不隐式取消当前播放器。抢话/人工停止必须显式 cancel 当前动作，渠道负责执行，不推导策略。无房间/旧会话/过期拒绝时也关闭未开始消费的源。

音量更新和 push 在同一个 output lane 串行，不能持该 lane 等待取消后会反过来获取它的任务。退役流程先撤销 admission、取消正在等待 source 的 reader，再等待在途 transport push，并调用 stop；所有操作使用原 binding 引用。无法确认停止即返回 unsafe，由会话层隔离绑定，禁止新房间复用。输入 detach 先撤回 callback/owner 标识，再停 pump/清理窗口；即使 SDK 迟到回调也无法进入新会话。

容量是本次目标设计值，尚未实测：首批实现须让它们成为经过验证的有限配置/内部常量，并覆盖边界；不能称现有代码已有字节上限。性能调优可改变容量，不改变丢帧、关闭与结果语义。

## 证据、迁移和验证

已核实 `VoiceDuplex.enqueue_remote_pcm/_enqueue_on_owner/close/push_tts_pcm/wait_tts_complete` 与 SDK `BrowserVoiceTransport.wait_tts_complete`：现有输入队列为 200 帧，跨线程交接到 owner loop；等待尾部实际播放与推送返回值不同。`VoiceAgent._wire_live/_leave_locked` 目前混合模型关闭和播放器锁，迁移必须保留先停旧 reader 再清输出的死锁保护。

最小切片先迁 session-bound push/stop 屏障，再迁完整音频 pump、播放完成、音量与静音。旧生成回调只能接渠道公开执行入口；临时 legacy 适配不能返回 duplex/transport 给调用者。

现有基线：`tests/test_tts_completion.py`、`tests/test_voice_duplex_rebind.py`、`tests/test_voice_output_controls.py`、`tests/test_voice_output_volume.py`、`tests/test_vad.py`。落地时增加源正常/失败 EOS 区分、最后尾音未完不得 completed、部分播放未知范围、重复/缺口帧、超大 PCM、慢源、取消自身再取消、输入跨线程关闭、输出阻塞中切房、满队列断点的公开行为测试。保持真实内部协作，只替换外部 transport 和可控音频源；本页不是新测试通过报告。
