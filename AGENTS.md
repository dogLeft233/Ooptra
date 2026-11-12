# 仓库贡献指南

探索或修改代码前，先阅读 [ARCHITECTURE.md](ARCHITECTURE.md)，了解渠道交互、上下文管理、行为决策、对话生成、系统维护五模块的职责、公开接口、状态归属与应用流程。接口以代码为准；变更影响职责、接口或运行路径时同步更新架构文档。

## 文档语言与索引

本项目文档统一使用简体中文，包括新增或修改的 README、架构说明、操作指南、设计方案及开发计划。命令、路径、代码标识符和必要的技术名称保留原文。新增或移动文档时，同步维护本节索引。

按任务阅读 `docs/` 中的相关文档：

| 文档或目录 | 内容与阅读时机 |
| --- | --- |
| [代码要求与开发模板](docs/coding-standards.md) | 新增或修改代码、测试、日志、lint 配置及 Git 提交、推送、PR 操作时阅读 |
| [代码审查要求](docs/code-review.md) | 审查代码或修改输入解析、领域类型、日志及异常处理时阅读 |
| [工程规范与模板选型依据](docs/engineering-sources.md) | 调整工程规范或评估外部模板、工具时参考 |
| [配置与维护指南](docs/operations.md) | 修改配置、启动方式、环境变量、日志排障、更新或数据备份时阅读 |
| [语音与 API 指南](docs/voice-guide.md) | 修改语音后端、播放控制、进退房、自动串门或 HTTP API 时阅读 |
| [双机器人语音联调验收](docs/bot-pair-acceptance.md) | 修改语音采集、VAD、模型后端、推流、成员映射、监听白名单、回合限额、进退房或多实例运行时阅读并核对验收 |
| [AstrBot 上下文同步](docs/astrbot-context-sync.md) | 修改人格、会话来源、记忆同步或插件交互契约时阅读 |
| [Pyright 诊断修复记录](docs/pyright-audit.md) | 处理类型检查及相关运行边界问题时参考 |
| [三渠道同步审查](docs/repository-audit.md) | 检查历史审查范围、修复背景和发布验证约束时参考 |
| [渠道子模块设计](src/channels/README.md) | 修改平台适配、房间状态机、音频输入/输出、执行台账时，经子模块导航深入 |
| [上下文子模块设计](src/context/README.md) | 修改情境投影、事务历史/outbox、来源/授权或 AstrBot RPC 时阅读 |
| [行为子模块设计](src/behavior/README.md) | 修改接话尝试/冷却、Live 等待、串门及主动调度时阅读 |
| [生成子模块设计](src/dialogue/README.md) | 修改级联 job、Live 会话、工具端口或输出证据协议时阅读 |
| [维护子模块设计](src/operations/README.md) | 修改配置事务、备份边界、任务交接或失败恢复时阅读 |
| [应用装配与流程](src/app/README.md) | 修改跨模块任务、输入路由、工具端口适配或管理面停止顺序时阅读 |
| [模块边界与接口演练](docs/superpowers/specs/2026-11-08-module-boundaries.md) | 设计模块接口、核对依赖与推演级联/Live/取消流程时阅读 |
| [五模块重构准备](docs/superpowers/plans/2026-11-08-module-refactor-readiness.md) | 开始重构、确定迁移批次、检查现状证据与验收门槛时阅读 |
| [设计方案目录](docs/superpowers/specs/) | 查阅自动串门、语音与维护功能的设计背景 |
| [实施计划目录](docs/superpowers/plans/) | 查阅功能实施、验证、渠道同步和发布的历史计划 |

内部详细设计通过所属模块 README 的子模块索引维护，不在顶层复制全部接口。审计记录和历史方案按其记录时点理解；当前行为以代码、配置及测试核实，存在差异时明确说明。

项目级 skill：[深模块架构设计](.agents/skills/design-deep-modules/SKILL.md)。设计或审查模块划分、公开接口、状态归属，或维护及验证架构文档时读取，执行其中的禁止事项与自查；普通局部修复不因此扩大为全库重构。

项目级 skill：[Prove It](.agents/skills/prove-it/SKILL.md)。交付前验证、同一问题被再次纠正，或编写与清理测试时读取。

## 项目结构与模块组织

Ooptra 基于 Python 3.10+，连接 Oopz 与 OneBot v11/AstrBot，并提供实时语音和 Web 控制台。

- `main.py` 是应用入口；`launcher.py` 支持受管启动与更新。
- `src/channels/`、`context/`、`behavior/`、`dialogue/`、`operations/` 是五个业务模块；先读各自导航与公开接口。
- `src/app/` 负责装配、跨模块流程及管理请求；`src/core/` 提供共享机制。平台与协议适配在渠道内部，退休命名空间由静态检查拦截。
- `src/webui/` 包含控制台路由与前端资源；根目录 `assets/` 存放文档配图。
- `src/oopz_sdk/` 是内置上游 SDK。保留其现有代码风格，兼容性修复需通过回归测试验证。
- `tests/` 存放 Python 测试，`tests/browser/` 存放浏览器检查，`docs/` 存放操作与开发文档。

## 模块导航与目录约束

新增能力或迁移实现时，从 [架构中的模块导航](ARCHITECTURE.md#接口与数据契约) 选择模块，先读其 `README.md` 和已实现的 `api.py`、`models.py`，再进入 `_internal/`。遵循 [目录规则](docs/coding-standards.md#模块目录与接口导航)；公开契约包含效果、失败、容量和取消语义，修改时同步实际调用者。测试归属与执行入口见 [测试导航](tests/README.md)。

## Codegraph 代码查询

本仓库已初始化 `.codegraph/`。查询代码结构、符号定义、调用关系或评估修改影响时，优先使用 Codegraph 的 `codegraph_explore`，再根据结果补充定向搜索。

- `projectPath` 显式传入当前仓库根目录的绝对路径，避免查询到其他项目。
- `query` 优先使用真实符号名或简短代码术语，例如 `BridgeController`，或用 `VoiceRuntime VoiceAgent bind_bot` 追踪跨模块关系；用 `maxFiles` 限制返回范围。
- 先查看返回的源码、调用路径、调用方与关联测试，再确定修改位置；已返回的完整源码无需重复读取，截断部分按符号名继续查询。
- 查询无结果时，先缩小为准确的类名或函数名。工具不可用、索引缺失或结果与当前代码不一致时，使用 `rg` 和定向文件阅读核实；图中未显示的关系不等于不存在，尤其是动态注册与回调。

## 构建、测试与开发命令

激活虚拟环境后，在仓库根目录执行：

- `python -m pip install -r requirements-dev.txt`：安装运行依赖和开发工具。
- `cp config.example.py config.py`：首次检出时初始化本地配置。
- `python launcher.py`：启动应用，控制台默认地址为 `http://127.0.0.1:3090`。
- `python -m pip install -r requirements-optional.txt` 和 `python -m playwright install chromium`：安装可选语音及浏览器功能所需依赖。
- `python scripts/check_architecture.py`：检查模块目录与公开接口导入约束。
- `ruff check .` 和 `pyright`：执行代码规范与类型检查。
- `npm ci` 和 `npm run lint:js`：安装锁定的开发工具并检查正式 JavaScript 与浏览器测试（Node.js 22.13+ 的 22.x 或 24+）。
- `python -m pre_commit install`：可选安装本地提交检查；`python -m pre_commit run --all-files` 手动运行，使用已激活的开发虚拟环境。
- `pytest -v -o faulthandler_timeout=60`：运行 Python 测试套件。
- `python scripts/build_webui_preview.py`：前端修改后重新生成独立预览；添加 `--check` 检查预览是否同步。

## 代码风格与命名约定

Python 使用四空格缩进，函数和模块使用 `snake_case`，类使用 `PascalCase`。遵循 Ruff 配置：目标版本为 Python 3.10，行宽为 100 字符，并检查导入排序。Pyright 全局使用标准模式，解析边界按 `tool.pyright.strict` 渐进启用严格检查；两者均排除内置 SDK。前端沿用现有风格：JavaScript 使用两空格缩进，函数使用 `camelCase`。

## 测试要求

pytest 文件命名为 `test_<behavior>.py`，测试函数使用描述性的 `test_*` 名称。通过 `tmp_path`、`monkeypatch`、合成凭据和模拟服务隔离回归测试。开发时先运行相关测试，提交前运行完整套件。当前未配置数值化覆盖率门槛。

| 规则 | 由什么拦住 |
| --- | --- |
| 默认测试收集只进入 `tests/`；真实联调脚本单独按验收指南执行，禁止用 `pytest .` 扩大收集 | `pyproject.toml` 的 `testpaths` + `tests/architecture/test_test_discovery.py`；显式指定其他路径仍需人工核对 |

界面变更还需运行 `tests/browser/` 下的 Playwright 脚本；依赖安装方式与执行命令见 `.github/workflows/ci.yml`。

语音链路或多实例运行的变更，还需按 [双机器人语音联调验收](docs/bot-pair-acceptance.md) 核对适用路径，记录真实双向接话、轮数限制和两端退房证据。未实测的路径明确标为未验证；验收结论注明版本与证据范围。

## 提交与 Pull Request 要求

Agent 按 [Git 仓库工作流程](docs/coding-standards.md#git-仓库工作流程) 检查已有改动、拆分完整提交、验证快照并核对远端 CI 与合并门禁。

当前浅克隆历史仅包含一条描述性合并提交，无法据此确定提交前缀惯例。提交标题应简洁，以动作描述变更。PR 需说明行为变化、关联相关问题并列出验证结果；界面变更附截图，涉及预览时同步重新生成。

## Code Review Rules

人工、Codex GitHub Code Review 和其他代理审查均先读 [代码审查要求](docs/code-review.md)，以 [代码要求](docs/coding-standards.md) 为规范来源，以 PR 的需求与当前 head SHA 固定范围。按变更路径执行适用章节，分别报告需求符合情况与规范符合情况；明确缺陷、设计改进和验证缺口按该文档的报告格式区分。代码位置、触发条件与影响必须可核实。

完整读取代码要求，按 [完整规范覆盖表](docs/code-review.md#审查目标与完整规范覆盖) 逐章记录适用性和证据。审查以编码约定、模块化与信息隐藏为重点，行为正确性用适用场景测试验证；发现可信错误路径仍须报告。修复及复审按 [收敛流程](docs/code-review.md#修复与收敛) 执行，完整规范报告独立于 GitHub 高优先级行内意见保留。

### 职责混杂（ARCH-RESP）

检查本次新增或扩展的类、文件是否同时掌握多个独立变化的业务决策，尤其是渠道协议、接话策略、模型生成、历史/记忆提交和维护配置是否被混入同一协调对象。重点追踪大文件及 `app`、`core`、HTTP 路由中的业务判断；给出至少一条实际调用路径和需要共同修改的职责，说明应保留的接口与迁移方向。按知识与变化原因判断拆分，文件行数只作线索；有实际协议转换价值的薄适配可保留。

### 状态归属（ARCH-STATE）

对变更涉及的每份权威可变状态，追踪创建、读取、修改及清理入口，确认只有一个拥有者。检查跨模块直接修改私有状态、重复计费/决策、可变快照泄漏，以及旧会话、取消、失败和迟到回调是否能改写新状态；生成完成、实际播放、本地提交和外部确认须各有真实证据。发现问题时指出当前写入者、应有拥有者与可构造的错误顺序；复制的只读快照不等于重复权威状态。

### 模块边界（ARCH-BOUNDARY）

对照当前分支的 `ARCHITECTURE.md`、所属模块 README、已实现的公开接口及类型，检查静态导入和运行时调用。报告越界获取 SDK/数据库句柄/可变内部对象、动态绕过公开入口，以及要求调用者拼装模块内部锁、代次、重试或资源收尾的接口；说明隐藏知识应归哪个模块、哪些真实调用者受影响。跨模块反馈应通过明确的公开端口与装配注入连接，迁移兼容边界按该 PR 已声明的范围核对。

### 架构审查证据与结论

以上三项是独立检查项，不能用 CI 成功、目录已移动或没有发现运行时 bug 代替。审查结果应逐项说明“发现问题 / 未发现问题 / 未覆盖”，附实际检查的位置和调用路径；未覆盖须说明缺少什么证据。每条意见引用 ARCH-RESP、ARCH-STATE 或 ARCH-BOUNDARY，按 `docs/code-review.md` 区分明确缺陷、设计改进与验证缺口。职责混杂或接口泄漏即使尚未复现故障，也可作为有具体证据的设计改进报告；不因此虚构严重缺陷或要求顺带全库重构。迁移期尚未承诺完成的能力单列剩余工作，不能把未来目标当作当前已实现的接口。

涉及外部输入时追踪解析结果直至副作用；涉及日志、异常或后台任务时核对原会话归属、失败观测及敏感信息。具体检查步骤统一维护在审查要求中。

提交审查请求后，按 [自动审查与结果核验](docs/code-review.md#自动审查与结果核验) 记录实际结果。CI 成功、请求评论、👀 接收标记和审查完成分别核对；没有当前提交的完成证据时，交付状态写“审查未完成”并说明阻塞原因。Draft 转正式、Auto-merge 和合并按 Git 工作流程及用户授权分别处理。

## 安全与配置

凭据存放在已被 Git 忽略的 `config.py`、`private_key.py` 或环境文件中。保留已有配置与 `data/`，尤其是 `data/onebot_v11.sqlite3` 中的身份映射。新增配置项时同步更新示例配置。
