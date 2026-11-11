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

表达任务覆盖单订阅、背压、首帧前关闭、失败EOS和真实HTTP/WS边界；输入、播放覆盖字节预算、断点、帧序号、取消及未知效果。`tests/app/test_output_commit_lifecycle.py` 验证已受理SQLite线程、切房/取消原scope提交及失败静默回执。配置/维护覆盖跨进程锁、CAS、部分应用、候选gate、handoff、父进程退出凭据与原子归档发布。

已移除的供应商复合入口、私有音频回调、global历史绑定和`.__new__`状态拼装测试，分别由新公开任务/真实应用协议测试承接。测试数量不是完成标准；删除旧实现耦合用例时应注明保留的行为和新验证入口。

共享画面：`tests/channels/test_screen_capture.py` 覆盖原会话、退房取消、帧年龄与 Oopz 数字发布者身份；`tests/browser/screen-frame.cjs` 执行真实 MediaStream 抽帧，`tests/browser/screen-viewer-lifecycle.cjs` 覆盖观看凭据刷新、清理代次与解码帧停滞；`tests/test_aliyun_deepseek.py` 验证当轮图像请求与 TTS 隔离。

语义接话：`tests/behavior/test_semantic_replies.py` 验证无抽样、叫名兜底和输入绑定；`tests/dialogue/test_reply_assessment.py` 验证真实HTTP与严格JSON边界；`tests/test_aliyun_deepseek.py` 贯通识别、语义候选、真实行为/上下文、生成与播放。外部ASR/TTS/LLM服务在本地协议服务器替代，真实叫名声学效果另行验证。

逐句排障：`tests/context/test_voice_traces.py` 验证真实SQLite分页、重启、输入身份、记忆隔离、有界受理及取消关闭排空；`tests/app/test_trace_write_isolation.py` 验证真实SQLite写锁不阻塞事件循环；`tests/app/test_live_unplayed_trace.py` 验证真实Live过期且未播放轮次诊断终态；`tests/app/test_voice_transcript_api.py` 验证认证HTTP与参数边界；`tests/test_aliyun_deepseek.py` 验证真实协议/应用采集、未选中输入的最终转写及结束原因；`tests/browser/transcript-timeline.cjs` 验证正式资源中的昵称、文字、原因、播放终态、分页、失败保留、迟到请求、XSS和窄屏。
