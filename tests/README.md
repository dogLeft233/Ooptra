# 测试导航

测试统一放在 `tests/`，按职责分组，与 [五模块架构](../ARCHITECTURE.md) 对齐。测试靠近对应模块的逻辑位置，保持与运行代码分离，不在各 `src` 目录内再建立另一套测试树。pytest 支持这种布局，参见 [官方说明](https://docs.pytest.org/en/stable/explanation/goodpractices.html#tests-outside-application-code)。

## 目标布局

```text
tests/
  channels/                 # 渠道事件、执行与资源回收
  context/                  # 情境、历史、记忆同步
  behavior/                 # 决策、调度、冷却与配额
  dialogue/                 # 生成契约、供应商适配与流式取消
  operations/               # 配置、备份、升级与恢复
  app/                      # 应用输出结果与历史提交
  integration/              # 多模块协作和回归路径
  acceptance/               # 真实联调控制器的隔离 HTTP 测试（不连接模型）
  architecture/             # 目录和依赖约束
  browser/                  # 现有 Playwright 检查
  support/                  # 确有多处复用的测试帮助代码
  conftest.py               # 少量全局准备；模块 fixture 放各自 conftest.py
```

目录按测试实际需要创建。当前五模块同名目录和 `app/` 均已包含公开契约测试；`acceptance/` 使用本地 HTTP 服务验证手动验收工具，不调用真实 Oopz/模型；`architecture/` 包含目录/依赖和测试发现检查；其余原有平铺测试保持原位，随对应实现逐批迁移。生成供应商使用本地 HTTP 模型服务检验协议和并发 sink；上下文使用真实隔离 SQLite 检验事务、重启、去重和来源授权；旧 JSONL/outbox 仅作为导入材料。新五模块代码的测试放到同名目录；平铺的历史测试按行为名称保留，但已退休的生产导入必须同步移除。测试布局要求由审查维护，架构脚本目前不冻结旧测试文件清单。

## 归属和测试接口

- 模块测试优先使用公开接口，验证输入、输出、状态变化与失败语义；有明确价值的内部算法或适配测试可以读取所属模块的 `_internal/`，不将其他模块私有对象作为 fixture。
- 一个测试主要验证哪个模块的承诺，就放在哪个目录。只有关注协作契约时放入 `integration/`，例如会话过期后停止生成与播放、实际结果进入历史、维护后重绑连接。
- 保留少量完整流程测试，补足取消、部分执行和外部回执；不要为每一层重复同一组调用次数断言。模拟真正的外部依赖或公开接口，避免大量模拟模块内部方法。
- fixture 尽量局部；跨目录复用确有需要时放入 `support/`，不要让测试相互导入。沿用当前 pytest 导入方式，文件名保持全套唯一，如 `test_behavior_idle_decision.py`，避免新增目录后同名文件发生导入冲突。
- 移动文件前检查其中依赖 `__file__` 的路径、浏览器脚本入口和 CI；移动后比较收集结果，确认未漏掉测试。

## 执行与验证

默认发现范围由 `pyproject.toml` 的 `testpaths = ["tests"]` 限定。`data/` 内的真实联调脚本不得在收集阶段执行；隔离回归 `tests/architecture/test_test_discovery.py` 会放入一个带导入副作用的模拟联调脚本验证此约束。不要为扩大覆盖执行 `pytest .`，它会显式绕过默认目录范围。

```bash
python -m pytest tests/architecture -q
python -m pytest --collect-only -q
python -m pytest -v -o faulthandler_timeout=60
```

模块迁入后可执行 `python -m pytest tests/<module>/ -q`。全套测试仍从根目录执行，浏览器命令见 `.github/workflows/ci.yml`，语音运行验收见 [双机器人联调](../docs/bot-pair-acceptance.md)。目录重排本身不能代替真实集成验证。


## 完整契约回归

PR 审查反例入口：[静音和长排空](channels/test_review_playback.py)、[Live 定稿、记忆授权及断连](app/test_live_input_finalization.py)、[级联控制原行动结算](app/test_cascade_control_identity.py)。均使用真实渠道、上下文或应用流程，仅替代外部平台和模型传输；不能替代真实房间及双 bot 声学验收。

表达任务覆盖单订阅、背压、首帧前关闭、失败EOS和真实HTTP/WS边界；输入、播放覆盖字节预算、断点、帧序号、取消及未知效果。`tests/app/test_output_commit_lifecycle.py` 验证已受理SQLite线程、切房/取消原scope提交及失败静默回执。配置/维护覆盖跨进程锁、CAS、部分应用、候选gate、handoff、父进程退出凭据与原子归档发布。

已移除的供应商复合入口、私有音频回调、global历史绑定和`.__new__`状态拼装测试，分别由新公开任务/真实应用协议测试承接。测试数量不是完成标准；删除旧实现耦合用例时应注明保留的行为和新验证入口。

`tests/operations/test_plan_lifecycle.py` 使用真实变更锁及子进程 watchdog 验证计划/配置并发无死锁和取消后的实际落盘。`tests/app/test_input_delivery.py`、上下文事务及Live定稿回归验证无输出输入的原来源采集、poll/resolve后迟到输出不重复用户句子和重启幂等。

PR3 的旧 TurnSubmission/JSONL 回归已映射到当前 SQLite 事务回滚、`test_output_commit_lifecycle.py` 的受理后取消及 `test_voice_history_outcomes.py` 的输入提交/Live drain取消，未保留退休生成入口或非原子双写约定。

后续上游双存储故障矩阵在当前单 SQLite 设计中映射为 `test_output_commit_lifecycle.py` 的真实投递 SQL 故障/取消/重试：已观察输入不丢失，失败输出事务回滚，原来源恢复采集一次。第九轮拥塞回归覆盖关闭后九句均只有用户且原scope投递各一次，不恢复旧 JSONL/outbox 双写能力。

Live 边界回归覆盖 Hold 跨晚转录窗口继续等待、无真实边界的硬超时/断连不提交半句、Alice/Bob 同轮输入无错误归属，以及旧 PreparedContext 工具授权撤销。使用真实 LiveFlow/ContextService 与本地协议替身；等待输入定稿不以播放结束替代。

同一回归入口还覆盖停止、退房、配置重建与账号重绑时的已完成 Hold 输入，以及事件未消费、来源已切换和真实 SQLite 写入失败后的资源回收与原来源重试。

PR3 最新生命周期回归迁入 `app/test_live_input_finalization.py`：首次播放确认前取消保留原输入，真实 SQLite 拒写仍关闭模型/停止播放/实际退房，以及管理等待者重复取消后等待受理写入并传播取消。退休 `TurnSubmission`/JSONL 入口不恢复；同一契约由当前公开流程和事务台账验证。
