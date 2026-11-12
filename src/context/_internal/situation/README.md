# 情境投影

实现在模块协调器 [service.py](../service.py)，公开 `ContextService.observe/snapshot`。投影拥有观察版本、连接/会话序号、近期去重和每房间独立状态；不读取渠道内部字段，不控制真实房间。

`Observation` 是已解析事实：事件 ID、原 SessionRef、连接 ID、源序号、类型及必要文本/成员。相同 ID 同内容无副作用，异内容冲突；同连接和会话的旧序号不覆盖新事实。显式 session 观察替换活跃会话后，迟到旧识别可入历史但不能改新投影；退役会话的 session 观察也不能复活旧投影。

成员快照独立保留；识别/活动更新不清空成员。发现序号缺口使完整性降级，只有新完整成员快照能恢复；缺信息不是空房间事实。快照与历史切片使用冻结具名类型。

完整识别先交台账按 input_ref 提交，成功后才安装投影；Live 增量 `complete=False` 不落半句话。近期观察去重和完整输入证据最多保留 4096 项；耐久输入幂等由 SQLite 保证。高频状态仅更新投影，音频不进上下文。

版本由 observation_version、source_generation、history_revision 组成；准备捕获版本，已知版本不匹配时明确拒绝。情境不是渠道真实状态的替代，执行仍由渠道复查。

验证见 `tests/context/test_transactional_context.py`：旧会话迟到、房间隔离、成员保留、缺序降级、增量与完整输入；历史存储替换不改变本公开观察接口。
