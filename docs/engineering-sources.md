# 工程规范与模板选型依据

检索日期：2026-11-08。本文记录一手资料及其适用范围，供调整工程规则时复核；日常代码要求见 [代码要求](coding-standards.md)，配置以仓库中的工具配置和 CI 为准。

## 结论与项目背景

Ooptra 已有 `src/`、pytest、Ruff、Pyright 和 GitHub Actions，核心复杂度来自 asyncio 生命周期、SDK 回调、房间状态与持久化恢复。适合沿用当前结构，补足可执行约束及局部代码示例。完整脚手架的框架、构建和发布假设需要单独评估，不能直接覆盖现有项目。

以下“原始建议”是来源的概括，“项目采用”是结合 [架构](../ARCHITECTURE.md) 作出的本地选择，二者不能等同。

## 公司公开工程资料

| 一手资料 | 原始建议 | 项目采用 |
| --- | --- | --- |
| [OpenAI：Harness engineering](https://openai.com/index/harness-engineering/)（2026-02-11） | 用短 `AGENTS.md` 导航到仓库知识库；把可重复检查的要求交给工具；让代理可以观察应用与反馈 | `AGENTS.md` 保留文档入口，详细规则放 `docs/`；通过 lint、类型检查、回归测试和浏览器检查反馈。该文是团队实验经验，不能据此省略本项目的发布验收 |
| [Google：代码审查关注点](https://google.github.io/eng-practices/review/reviewer/looking-for.html) | 审查设计、功能、并发、复杂度、有效测试与文档；避免预先泛化；将大范围格式修改与行为修改分开 | 重点审查任务关闭、迟到回调、会话归属及恢复路径；测试应能捕获真实失效；格式迁移单独处理 |
| [Google：小规模变更](https://google.github.io/eng-practices/review/developer/small-cls.html) | 提交保持小而完整，便于审查、合并和回退 | 新功能按可独立验证的行为拆分；同一行为的代码、回归测试和文档一起提交 |
| [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html) | 用 lint 消除常见错误；合理抑制误报并说明原因；明确资源释放和类型信息 | 保留现有 Ruff/Pyright；抑制限定到具体规则和位置。Google 的工具选型与行宽属于其组织约定，本项目继续使用自身配置 |
| [Microsoft：Pyright 渐进类型检查](https://github.com/microsoft/pyright/blob/main/docs/getting-started.md) | 逐步增加参数、实例变量及返回值类型，将类型检查放入 CI；可按文件提高严格程度 | 保留 standard 模式，优先补清模块接口、配置和外部输入类型；新建模块可独立评估更严格检查，避免以全局忽略消除诊断 |

## 模块设计依据

[Matt Pocock 的文章](https://www.aihero.dev/how-to-make-codebases-ai-agents-love) 与 [John Ousterhout 的 Modular Design 讲义](https://web.stanford.edu/~ouster/cgi-bin/cs190-winter18/lecture.php?topic=modularDesign) 的提炼和项目应用集中维护在 [深模块架构设计 skill](../.agents/skills/design-deep-modules/SKILL.md)。设计或审查模块边界时读取；它明确区分原文原则与项目选择，不将目录层级或文件数量作为架构质量标准。

## 模板比较

| 模板或配置来源 | 可以借鉴 | 对 Ooptra 的裁剪 |
| --- | --- | --- |
| [Microsoft python-package-template](https://github.com/microsoft/python-package-template) | `src/`、`tests/`、工具配置集中在 `pyproject.toml`，以及 CI 与开发环境配置 | 适合作检查清单。其包构建、多个重叠 lint 工具和覆盖率门槛均需重新评估；本项目已具备代码结构，继续使用 Ruff + Pyright + pytest |
| [Astral uv 项目模板](https://docs.astral.sh/uv/concepts/projects/init/) | 区分应用与库，支持最小项目和不带构建系统的应用 | 可作为未来新工具的起点。本次保持现有 requirements 安装方式；包管理器迁移应单独验证启动器、更新流程和可选依赖 |
| [Astral Ruff 官方配置](https://docs.astral.sh/ruff/configuration/) | 统一声明 Python 目标版本、文件范围与 lint 规则 | 在现有 `pyproject.toml` 增量完善，维持 Python 3.10 兼容和内置 SDK 的风格边界 |

项目内最有价值的代码模板是与当前边界一致的短示例：输入验证后调用状态拥有者、可取消任务的创建与回收、通过 fixture 隔离外部依赖的回归测试。这些示例宜与代码要求放在一起，避免生成新的通用基类或目录层级。

## Lint 配置依据

1. **显式选择并渐进增加规则。** [Ruff 规则选择指南](https://docs.astral.sh/ruff/linter/#rule-selection) 建议明确列出规则集、分组扩展，并谨慎使用 `ALL`，因为升级会隐式启用新增规则。项目现有错误检查、导入排序、bugbear、Python 兼容升级与简化规则应作为基线，新增规则先运行并评估误报。
2. **区分自动修复的安全等级。** [Ruff 修复安全性](https://docs.astral.sh/ruff/linter/#fix-safety) 区分 safe 与 unsafe 修复；后者可能改变行为。项目只在审阅 diff 并验证后采用修复，涉及兼容性和资源生命周期的修复需要针对性测试。
3. **显式文件也应遵守排除边界。** [Ruff `force-exclude`](https://docs.astral.sh/ruff/settings/#force-exclude) 可让命令行显式传入的文件仍遵守排除规则，适合编辑器或提交工具传文件列表的场景。项目内置 SDK 的批量检查排除应在这些入口保持一致，SDK 兼容性修改仍需回归测试。
4. **格式与语义检查分工。** [Ruff formatter 兼容说明](https://docs.astral.sh/ruff/formatter/#conflicting-lint-rules) 列出与格式化冲突的规则，也说明 formatter 不保证所有行均满足行长规则。项目应明确格式化范围，再启用检查；避免单次功能修改造成整库格式噪声。
5. **前端按执行环境配置。** [ESLint 配置文件指南](https://eslint.org/docs/latest/use/configure/configuration-files) 支持按文件匹配配置。浏览器脚本与 Node 浏览器测试使用不同全局变量；原生脚本也应保留其真实脚本类型，不能为通过 lint 假定全部采用 ES modules。

## 工具无法替代的项目检查

补充原则：[Alexis King：Parse, don't validate](https://lexi-lambda.github.io/blog/2019/11/05/parse-don-t-validate/) 强调把解析获得的信息保留在返回类型中。本项目据此要求外部输入转换为领域对象，具体要求见 [代码要求](coding-standards.md#输入解析保留已经确认的信息)，审查步骤见 [代码审查要求](code-review.md)。这是一项设计原则，并非现有 lint 能完整证明的性质。

[Pyright 配置](https://github.com/microsoft/pyright/blob/main/docs/configuration.md) 支持用 `strict` 路径列表逐模块启用严格检查，本项目先用于语音控制解析模块；[Ruff PGH003](https://docs.astral.sh/ruff/rules/blanket-type-ignore/) 要求类型忽略提供诊断编号，用于减少无边界的检查绕过。两者只能提供部分约束，构造器不变量与调用方消费仍需测试和审查。

以下是从 Ooptra 架构推导的本地要求，不是上述公司统一规定：

- 语音进退房、桥接重建和自动串门修改，要验证旧任务不会操作新会话，取消后资源能被回收。
- 配置编辑遵循已有锁与替换流程，SQLite 映射和用户数据在更新、备份及恢复中保持连续。
- SDK 音频回调进入事件循环时保持既有线程交接及有界队列语义。
- AstrBot 待写事件继续使用创建时的来源范围，回执决定持久化结果。
- WebUI 变更检查真实交互及生成预览；单独 lint 通过不能证明浏览器行为正确。

关于 asyncio，Python 3.10 官方文档说明 [`create_task` 需要保留任务引用](https://docs.python.org/3.10/library/asyncio-task.html#asyncio.create_task)，并展示 [`cancel` 后等待任务与清理资源](https://docs.python.org/3.10/library/asyncio-task.html#asyncio.Task.cancel) 的行为。代码示例应以本项目最低 Python 版本为准，不能直接要求较新版本才提供的任务管理 API。

## 日志与可观测性依据

[Python 3.10 Logging Cookbook](https://docs.python.org/3.10/howto/logging-cookbook.html) 提供日志上下文、`QueueHandler` / `QueueListener` 与多进程输出方案。本项目据此要求集中配置、明确关联上下文和写入生命周期；当前运行实现仍为同步文本 handler。

[OpenAI Harness engineering](https://openai.com/index/harness-engineering/) 描述隔离 worktree 中可查询日志、指标和调用链的验证闭环，属于具体团队实践。本项目借鉴可复现与可关联的排障方式，暂不将整套观测基础设施列为运行依赖。[Vector 管道概念](https://vector.dev/docs/introduction/concepts/) 区分采集、转换和发送；采集器不能补出业务未记录的上下文。

[Ruff G101](https://docs.astral.sh/ruff/rules/logging-extra-attr-clash/) 检查 `extra` 与保留字段冲突，[PLE1205](https://docs.astral.sh/ruff/rules/logging-too-many-args/) 检查格式参数数量。项目显式选择日志规则，范围与人工审查要求见 [代码要求](coding-standards.md#日志与可观测性)。
