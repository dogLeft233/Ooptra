# 音频输入与会话输出

属于 [渠道交互](../../README.md)。公开入口在 [api.py](../../api.py)，本目录按输入切句、duplex 传输、输出控制和播放台账组织，避免应用或供应商各写一套播放器。

## 输入

`duplex.py` 将跨线程 PCM 交给所属 loop。跨线程待交接和 loop 队列分别限制 200 帧/4 MiB，单帧最多 64 KiB；仅接受有效采样率和完整 PCM16 样本。队列满载或非法帧标记该来源断点，旧代次帧丢弃，下一有效帧之前通知重置切句。关闭撤回回调身份，旧捕获闭包不能进入新会话。

`input.py` 拥有监听白名单、来源归一化、每成员 VAD 和输入序号。最多 128 个成员窗口、VAD 总暂存最多 4 MiB；超预算清除窗口并报告 gap。`generation` 标记窗口重置；`UtteranceResult.stream_pcm16` 提供本次处理的有声起点后增量（含断句静音），只有 ready 的 `pcm16` 是裁尾完整句。切句区分 ready / accumulating / noise / too_short / gap，不将不连续采样拼成完整输入。采样率变化重建该来源窗口；噪声/最小时长属于输入质量，语义控制后的回应资格归行为。

`vad.py` 隐藏有声/静音窗口和尾部裁剪；返回完整有声段。空 PCM 不表示生成完成，外部切句也不等于模型识别出了文字。

## 播放与真实完成

`playback.py` 接管 `PlaybackRequest` 和异步 `OutputFrame` 源。每会话一个播放器，pull 流提供背压；每帧验证 action/session/output ID、从零连续的 frame_seq、固定采样率、单声道 PCM16 和大小。源、push、finish 和 drain 均受期限约束。

```text
Pending → Playing → Draining → Completed
   ↓          ↓          ↓
Rejected / Expired / Cancelled / Failed / Partial
```

只有源正常终态、finish 成功、实际 drain 确认后才 Completed。`GeneratedAudio` 把生成失败/缺终态转换为 `OutputSourceError`；执行器不会当 EOS 后照常 finish。已可能送入浏览器的失败记录 unknown_samples，不把整段转录当已播放。

同 ID 重等不再消费新流，同一活跃源重复等待不能提前关闭它；提前拒绝、未开始取消、消费异常和正常完成都收回源。首次拥有者取消撤销动作，重复等待者取消只撤销等待。取消自身被打断仍保留有拥有者的清理；查询返回原台账结果。

停止未确认、源/关闭不合作或在途 push 未收尾时禁止继续播放。退役先取消等待源的 reader，再等真实 in-flight 传输，最后 stop；不持会话短状态锁反向等待自己。每次等待返回后复查截止和会话，旧帧不能进入新绑定。

## 音量、静音和持久化

`controls.py` 拥有实际音量/临时静音，更新在控制锁内串行。相对音量策略由行为解析为目标，不在渠道重复解释自然语言。异步保存端口由维护装配注入；磁盘保存成功与扬声器增益/房间状态广播分别报告。调用者取消已受理保存后仍观察落盘工作结束。

Duplex 不向模块外暴露浏览器 transport。操作失败报告安全原因，实际保存/应用状态通过结果和快照查询；取消不是回滚。

## 验证

[输入契约](../../../../tests/channels/test_input_contract.py)、[duplex 生命周期](../../../../tests/channels/test_duplex_lifecycle.py)、[播放契约](../../../../tests/channels/test_playback_contract.py)和原 VAD/排空/音量回归，只替代外部 PCM/浏览器传输。新增反例实际证明字节总预算、跨线程断点、未开始取消、源失败、deadline、未知范围和停止复用屏障；生产声学效果仍需真实双 bot 验收。
