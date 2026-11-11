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

目录按测试实际需要创建。当前五模块同名目录和 `app/` 均已包含公开契约测试；`acceptance/` 使用本地 HTTP 服务验证手动验收工具，不调用真实 Oopz/模型；`architecture/` 包含目录/依赖和测试发现检查；其余原有平铺测试保持原位，随对应实现逐批迁移。生成供应商使用本地 HTTP 模型服务检验协议和并发 sink；上下文使用真实隔离文件检验恢复与提交。新五模块代码的测试放到同名目录；旧路径修复可以继续使用其原测试文件。测试布局要求由审查维护，架构脚本目前不冻结旧测试文件清单。

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

历史收尾：`tests/test_voice_history_outcomes.py` 覆盖非流式识别后等待 chat/TTS 时停止，仅保留原用户输入；Live 公告在渠道成功确认后仅追加助手文本。

提交取消窗口：`tests/test_voice_history_outcomes.py` 用真实阻塞 JSONL 写入覆盖流式/非流式 stop 后仅一份输入/助手及一个 outbox 请求；`tests/context/test_turn_submission.py` 检查取消同时磁盘失败仍被观察且不重复受理。

Live 排空取消：`tests/test_voice_history_outcomes.py` 经真实模型消息、阻塞渠道排空与真实 JSONL/outbox，覆盖停止、退房及来源刷新，确保仅保留原scope输入。阻塞提交回归通过取消事件握手同步，兼容 Python 3.10，不依赖 `Task.cancelling`。

两存储故障矩阵：`tests/context/test_turn_submission.py` 使用真实不可写目标路径覆盖外部失败、本地失败、双失败及取消/重复等待；`tests/test_voice_history_outcomes.py` 用九轮真实Live消息和八个阻塞排空确认，验证满载输入背压及九份原scope历史/outbox，无未确认助手记录。
