# 仓库贡献指南

探索或修改代码前，先阅读 [ARCHITECTURE.md](ARCHITECTURE.md)，了解子系统职责、运行路径与状态归属。变更影响这些边界或路径时，同步更新架构文档。

## 文档语言与索引

本项目文档统一使用简体中文，包括新增或修改的 README、架构说明、操作指南、设计方案及开发计划。命令、路径、代码标识符和必要的技术名称保留原文。新增或移动文档时，同步维护本节索引。

按任务阅读 `docs/` 中的相关文档：

| 文档或目录 | 内容与阅读时机 |
| --- | --- |
| [代码要求与开发模板](docs/coding-standards.md) | 新增或修改代码、测试、lint 配置及准备提交时阅读 |
| [代码审查要求](docs/code-review.md) | 审查代码或修改外部输入解析、领域类型及消费方时阅读；逐入口追踪解析结果与构造不变量 |
| [工程规范与模板选型依据](docs/engineering-sources.md) | 调整工程规范或评估外部模板、工具时参考 |
| [配置与维护指南](docs/operations.md) | 修改配置、启动方式、环境变量、更新或数据备份时阅读 |
| [语音与 API 指南](docs/voice-guide.md) | 修改语音后端、播放控制、进退房、自动串门或 HTTP API 时阅读 |
| [AstrBot 上下文同步](docs/astrbot-context-sync.md) | 修改人格、会话来源、记忆同步或插件交互契约时阅读 |
| [Pyright 诊断修复记录](docs/pyright-audit.md) | 处理类型检查及相关运行边界问题时参考 |
| [三渠道同步审查](docs/repository-audit.md) | 检查历史审查范围、修复背景和发布验证约束时参考 |
| [设计方案目录](docs/superpowers/specs/) | 查阅自动串门、语音与维护功能的设计背景 |
| [实施计划目录](docs/superpowers/plans/) | 查阅功能实施、验证、渠道同步和发布的历史计划 |

审计记录和历史方案按其记录时点理解；当前行为以代码、配置及测试核实，存在差异时明确说明。

## 项目结构与模块组织

Ooptra 基于 Python 3.10+，连接 Oopz 与 OneBot v11/AstrBot，并提供实时语音和 Web 控制台。

- `main.py` 是应用入口；`launcher.py` 支持受管启动与更新。
- `src/bridge/`、`src/onebot_v11/` 和 `src/oopz/` 负责集成与协议转换；`src/core/` 提供共享基础能力。
- `src/voice_agent/` 包含语音会话、模型后端和房间自动化。
- `src/webui/` 包含控制台路由与前端资源；根目录 `assets/` 存放文档配图。
- `src/oopz_sdk/` 是内置上游 SDK。保留其现有代码风格，兼容性修复需通过回归测试验证。
- `tests/` 存放 Python 测试，`tests/browser/` 存放浏览器检查，`docs/` 存放操作与开发文档。

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
- `ruff check .` 和 `pyright`：执行代码规范与类型检查。
- `npm ci` 和 `npm run lint:js`：安装锁定的开发工具并检查正式 JavaScript 与浏览器测试（Node.js 22.13+ 的 22.x 或 24+）。
- `python -m pre_commit install`：可选安装本地提交检查；`python -m pre_commit run --all-files` 手动运行，使用已激活的开发虚拟环境。
- `pytest -v -o faulthandler_timeout=60`：运行 Python 测试套件。
- `python scripts/build_webui_preview.py`：前端修改后重新生成独立预览；添加 `--check` 检查预览是否同步。

## 代码风格与命名约定

Python 使用四空格缩进，函数和模块使用 `snake_case`，类使用 `PascalCase`。遵循 Ruff 配置：目标版本为 Python 3.10，行宽为 100 字符，并检查导入排序。Pyright 全局使用标准模式，解析边界按 `tool.pyright.strict` 渐进启用严格检查；两者均排除内置 SDK。前端沿用现有风格：JavaScript 使用两空格缩进，函数使用 `camelCase`。

## 测试要求

pytest 文件命名为 `test_<behavior>.py`，测试函数使用描述性的 `test_*` 名称。通过 `tmp_path`、`monkeypatch`、合成凭据和模拟服务隔离回归测试。开发时先运行相关测试，提交前运行完整套件。当前未配置数值化覆盖率门槛。

界面变更还需运行 `tests/browser/` 下的 Playwright 脚本；依赖安装方式与执行命令见 `.github/workflows/ci.yml`。

## 提交与 Pull Request 要求

当前浅克隆历史仅包含一条描述性合并提交，无法据此确定提交前缀惯例。提交标题应简洁，以动作描述变更。PR 需说明行为变化、关联相关问题并列出验证结果；界面变更附截图，涉及预览时同步重新生成。

## 安全与配置

凭据存放在已被 Git 忽略的 `config.py`、`private_key.py` 或环境文件中。保留已有配置与 `data/`，尤其是 `data/onebot_v11.sqlite3` 中的身份映射。新增配置项时同步更新示例配置。
