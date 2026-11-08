# 实时模型会话

[公开 API](../../api.py) 的 `LiveSession` 是持续输入、提议/许可与输出任务边界；[session.py](session.py) 拥有本地轮次及闸门，[gemini_live.py](gemini_live.py) 只拥有连接、setup与协议编解码。概率、窗口、控制授权由行为负责，真实播放由渠道负责，权威历史与工具授权由上下文负责。

## 绑定与生命周期

`open_live` 接收真实渠道会话与不可变准备上下文。上下文来源或渠道改变时必须关闭旧对象；`update_context` 仅接纳同session/source_generation/binding_id的新快照，pending轮次可更新，已批准轮次保留原上下文/工具授权。

会话 opening → ready → failed / closed。连接丢失立即清ready并使原对象failed，不自动在旧轮次上复活输出；应用读取snapshot后重建。再次open时会收尾失败旧绑定，正常活跃绑定禁止并行占用。aclose撤销任务/提议、等待输出与工具收尾并关闭socket。失败原因可经snapshot查询，不依赖满队列投递终态。

provider reader只处理消息和有界投递，不等待行为许可、渠道drain或外部记忆RPC。控制事件单订阅128项，满则fail-closed并关闭旧传输；任务异常被观察，不遗留无人读取异常。

## 提议、许可和输出

首个可识别轮次建立稳定ProposalRef，转录修订递增revision。pending提议的硬截止为LiveLimits晚转录窗口（默认2秒）；Hold不能延长该截止。Allow后采用独立表达期限，至多60秒并受intent.deadline约束。期限前provider已经完成的缓存仍可获批；没有可确认轮次边界的未决超时会关闭旧连接，避免把迟到输出误当新轮。

Allow绑定一次action；相同action重复返回原绑定，另一个action冲突，旧revision/过期/拒绝轮次不能复活。获批才发output_ready，ExpressionJob经相同GeneratedAudio接口交渠道；provider完成不等于扬声器排空。

候选及获批音频累计预算默认4MiB，片段先切为不超过64KiB的完整PCM采样。慢输出形成有界背压；溢出失败，不能静默丢中间音频却报告成功。没有许可的音频不能离开session。

客户端interrupt撤销当前任务并丢弃旧轮迟到输出，直到真实turnComplete再接受下一轮；NO_INTERRUPTION配置下不把audioStreamEnd误用于抢话。服务端interrupted同样终结输出任务，渠道清扬声器仍需独立取消确认。显式speak通过同一批准任务，不另造播放器；发送失败撤销原许可，不影响后续自动轮次。内部台词不执行模型提出的控制或记忆写入。

## 控制工具

ControlProposed携已解析候选、稳定ToolRef和触发ID。非法参数、未知工具、同包互斥退房/音量全部拒绝并回传拒绝事实。重复provider工具ID关联原候选/结果，完成后重复返回原实际回执，不再次执行。

应用在行为授权后、渠道副作用前同步claim_control；旧连接、已取消或已解决候选返回stale，同action重复幂等。Provider取消交付ControlCancelled；已受理副作用不能声称被取消，应用继续查询/记录实际结果。

resolve_control仅转换实际结果，不执行SDK。scheduled仅表示已安排，ok为false；executed且实际ActionOutcome完成才可宣称成功。退房导致旧socket关闭时不重发动作以追求工具ACK。待处理表128项，优先回收已解决记录，未决满载失败关闭，幂等范围不跨进程。

## 记忆工具

每个工具任务捕获绑定PreparedContext签发的工具端口，模型参数不能选择来源或grant。真实provider call ID传给上下文作为幂等身份。记忆工具并发4，结果缓存64，同ID并发复用、完成重发，异载荷拒绝。取消撤销等待及未提交工作，已受理耐久写保留unknown/queued事实；关闭等待任务回收，旧结果不会发送到新连接。

## 验证

[公开许可测试](../../../../tests/dialogue/test_live_permissions.py)守未授权零输出、重复/修订许可、完成后的晚转录、客户端打断、容量与期限；[连接生命周期](../../../../tests/test_gemini_session_lifecycle.py)、[工具控制](../../../../tests/test_voice_volume_intents.py)、[AstrBot真实事务工具](../../../../tests/test_astrbot_live_context.py)验证对应边界。真实网络和 Oopz 音频质量仍由应用双机器人验收记录；测试夹具不是实际Gemini平台验收。
