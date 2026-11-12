# 代码要求与开发模板

适用于新增与修改的代码。先读 [架构地图](../ARCHITECTURE.md)，再定位状态拥有者和关联测试；公开工程资料及模板选型见 [依据记录](engineering-sources.md)。工具可执行的规则以 `pyproject.toml`、`eslint.config.mjs` 和 CI 为准，本文补充工具无法判断的约束。

## 修改边界

- 一个变更解决一个可验证的行为，代码、测试、示例配置和必要文档一起更新。大范围重排、格式化和包管理器迁移单独处理。
- 编码前确认本次模块归属、公开入口、唯一状态拥有者及受影响调用者；边界变化先解释隐藏的知识和调用者负担。交付时按 [完整规范覆盖表](code-review.md#审查目标与完整规范覆盖) 提供实际证据，不能仅以 CI 通过或已移入目录证明模块化完成。
- 路由负责鉴权后的输入校验、调用和响应转换；房间/播放操作交给渠道，配置和部署交给维护，跨模块请求由 `app` 协调。新增通用层前，先证明现有模块无法承载该职责。
- 持久化状态、观测快照和外部服务确认分别表达。入队、进程启动、WebUI 可访问都不能代替业务成功回执。
- 内置 `src/oopz_sdk/` 保持上游风格；兼容性改动应最小化，并通过对应测试验证。批量 lint 排除 SDK 不代表免于审查。

## 模块目录与接口导航

新业务代码按 [系统架构](../ARCHITECTURE.md) 的五模块归属组织。目标结构固定为：

```text
src/
  channels/                 # 渠道交互
  context/                  # 上下文管理
  behavior/                 # 行为决策
  dialogue/                 # 对话生成
  operations/               # 系统维护
  app/                      # 装配和流程协调，不承载业务策略
  core/                     # 共享技术机制，业务类型留在拥有者模块
  webui/                    # HTTP和前端适配
  oopz_sdk/                 # 内置上游SDK，只在渠道适配访问
```

每个业务模块采用相同的阅读入口：

```text
<module>/
  README.md                 # 职责、接口入口、状态归属、迁移状态
  __init__.py               # 显式导出公共工厂，导入时不启动任务或连接
  api.py                    # Protocol、公开操作及失败/取消/完成语义
  models.py                 # 按需添加：对外数据类型与构造不变量
  _internal/                # 实现、私有模型及实际需要的内部划分
    adapters/               # 按需添加：外部协议、模型、存储适配
```

- 五模块均已迁入实际运行能力；新增能力仍须有真实调用方，不提前创建空接口。公开接口以 `api.py` 为事实来源，README 保留导航与必要约定，避免长期维护两份接口清单。
- 外部调用者通过 `<module>.api`、`<module>.models` 使用契约；装配通过包顶层导出的工厂创建实现。公开名称显式导出，避免 `import *`。`api.py` 和 `models.py` 独立于 `_internal/`，通过参数注入依赖。
- 跨模块传递拥有者定义的公开类型。接收者需要不同形状时在自己的适配处转换，避免双向导入或将所有业务类型放进 `core`。跨模块契约形成环时调整类型归属与注入方向。
- 业务模块内部按实际复杂度分文件。先用简单文件组织；仅在具有独立职责、状态或替换需求时增加子模块。避免统一强加 `controller/service/repository` 多层转发或为每个类建立目录。
- 新增一级业务模块时先修改架构与目录检查规则，说明现有模块无法容纳该职责的原因。`app` 只协调，`core` 只存通用机制，二者避免形成新的业务中心。
- `bridge`、`oopz`、`onebot_v11`、`voice_agent` 已退休，不保留重导出兼容入口。渠道 SDK 适配只在 `channels/_internal/adapters/` 访问 SDK，HTTP 只调用公开能力。
- `python scripts/check_architecture.py` 检查一级目录、公开入口、跨模块私有导入、依赖方向、SDK位置和退休路径。本地钩子与 CI 使用同一检查，无旧模块迁移豁免；动态调用、属性泄漏和状态归属仍按 [审查要求](code-review.md) 核对。

测试集中在 `tests/` 并按模块分目录，具体归属和迁移规则见 [测试导航](../tests/README.md)。

## Python 与接口

- 兼容 Python 3.10；新接口标注参数与返回值。外部输入的解析与类型要求见下一节。
- 可选语音依赖在对应能力入口加载，缺失时返回可理解的错误；纯桥接模式仍应能导入和启动。
- 常规业务捕获可预期的异常；边界层需要兜底时记录上下文并返回明确失败。日志的级别、上下文和脱敏遵循下文「日志与可观测性」。
- 函数名描述动作和对象；注释解释时序、不变量、兼容原因及取舍。修改接口时同步调用方和测试，优先删除过时分支。

## 输入解析：保留已经确认的信息

采用 **Parse, don't validate**：外部输入在边界解析为表达业务约束的对象，成功时返回该对象，后续业务直接使用解析结果。审查步骤见 [代码审查要求](code-review.md)。

- HTTP JSON、模型工具参数、配置和持久化恢复数据属于外部输入。解析入口优先接收 `object`，校验形状、字段、允许值及字段间关系；输出命名的领域对象或可区分联合。失败明确抛出异常或返回可区分的错误结果。
- 将成功判定保存在返回值中，例如 `parse_volume_command(raw) -> VolumeCommand | None`。这里 `None` 仅表示没有指令，非法输入抛出 `ValueError`，不能与合法空结果混用。
- 后续代码接收解析对象；在 JSON 响应、持久化或既有协议适配边界再序列化。为兼容旧接口必须转回字典时，标明边界，避免在新业务接口中继续传播原始输入。
- 有限操作使用 `Literal`、枚举或可区分联合；互斥动作优先拆成不同变体。数值范围、跨字段约束等静态类型不能完整表达的条件，由构造器或经过验证的工厂保证。合法对象尽量不可变，避免解析后的数据被原始可变字典污染。
- dataclass、TypedDict、`NewType` 和类型注解本身不执行运行时检查；`frozen=True` 只限制常规赋值，也不验证字段。直接构造必须保持不变量，或者明确限制构造入口。Pydantic 模型也需配置符合协议的严格检查，尤其区分 `bool` 与 `int`、字符串与数值，避免默认转换悄悄放宽协议。
- `cast`、`Any` 和 `type: ignore` 不能代替解析。确需描述已核实的容器形状时，保留元素为 `object` 并解释证明依据；例如 `voice_control._command_fields` 只在确认字典后标注 `dict[object, object]`，随后仍逐字段解析。

接口模板沿用真实模块，避免另建平行示例：

```python
from behavior.models import VolumeCommand, parse_volume_command


def require_volume_command(raw: object) -> VolumeCommand:
    command = parse_volume_command(raw)
    if command is None:
        raise ValueError("此操作需要音量指令")
    return command  # 调用方继续传递 command，而不是 raw。
```

`src/behavior/models.py` 持有严格类型检查的语音控制解析类型：动作使用 `Literal`，构造器检查音量范围和退房/音量互斥关系。现有单一 `VolumeCommand` 仍用运行时约束表达 action/value 关系；类型检查尚不能从 `action == "set"` 单独推导 value 非空。生成的 `VoiceReply.control` 保留解析对象；供应商其他协议边界继续按需迁移，不把该试点视为全库完成。

新建或修改解析边界时，补充非法输入、直接构造绕过、解析结果被消费的回归测试；按实际约束覆盖布尔值混入整数、缺字段、未知操作、额外字段、越界及互斥组合。类型完整后将模块加入 `tool.pyright.strict`，验证调用方也接收具体类型。

## asyncio、语音和资源

- 创建任务时指定拥有者并保留引用，定义停止、异常观测和清理路径。取消后等待任务结束；`CancelledError` 经过清理后继续传播。Python 3.10 使用现有任务管理方式。
- 网络调用显式设置超时；重试设次数或总时限，并区分可重试读取与可能重复执行的动作。同步 HTTP、等待和耗时文件操作交给线程或异步接口，避免阻塞事件循环。
- 跨线程音频通过既有事件循环交接；队列保持有界并规定丢弃策略。操作代次、会话标识和访问标识用于拒绝旧任务，不能仅凭“当前已连接”判断任务仍有效。
- 锁内只做维持不变量所需工作。若必须跨网络等待持锁，说明顺序保证和等待上限；避免关闭流程等待持有同一把锁的任务。
- HTTP session、WebSocket、浏览器、数据库与音频资源由创建方关闭；新旧 bot 重新绑定时验证旧传输释放及迟到回调。

下面是可取消工作任务的最小模板，实际模块应复用现有拥有者；循环中的 `CancelledError` 应继续向外传播，只有发起取消的关闭方消费该结果：

```python
import asyncio
from contextlib import suppress


class Worker:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        # start/close 由所属事件循环中的生命周期入口串行调用。
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="worker")

    async def _run(self) -> None:
        raise NotImplementedError  # 替换为有界等待与业务操作，并按需 finally 清理资源。

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task  # 其他异常继续暴露给生命周期监督者。
```

## 日志与可观测性

以下要求适用于新增或修改的日志路径；现有实现与排障操作见 [维护指南](operations.md#日志与排障证据)。沿用标准库 `logging` 和 `core.logger_config` 的集中配置，业务模块优先使用 `logging.getLogger(__name__)`，已有命名 logger 可保持兼容。handler、格式与输出目标由配置层管理，业务函数不自行添加 handler 或调用 `basicConfig`。

| 级别 | 使用条件 |
| --- | --- |
| `DEBUG` | 排障细节、正常取消、打断、过期任务丢弃；高频事件汇总后记录 |
| `INFO` | 启停、连接状态变化、进退房及回合完成等关键生命周期结果 |
| `WARNING` | 可恢复的降级、重试或资源压力；记录尝试次数与下一步动作 |
| `ERROR` | 请求或任务最终失败，由负责处理失败的边界记录 |
| `CRITICAL` | 当前进程无法继续提供核心服务、需要立即介入 |

- 使用稳定的事件名描述结果，例如 `voice.turn.completed`；消息写明阶段、结果和必要上下文。采用 `logger.info("event=voice.turn.completed turn_id=%s duration_ms=%.1f", turn_id, elapsed_ms)` 这样的参数化写法，避免提前拼接动态消息。耗时使用单调时钟测量并标明单位；排障输出的昂贵计算先检查日志级别。
- 按所属路径复用已经确认的 `request_id`、`session_id`、`turn_id`、房间标识、后端名、操作代次或维护任务标识，使开始、完成、取消和失败能关联；只记录适用字段，不为每条日志生成新的关联标识。关联字段取自解析对象及任务创建时保存的上下文，迟到任务保留原会话归属，不能改记为当前房间。
- 当前 formatter 只输出消息，不自动呈现 `extra` 字段；必须可见的上下文先放入参数化消息。引入结构化 formatter 时再输出独立字段，并验证旧 WebUI 的日志筛选及多行堆栈显示。`extra` 不得覆盖 `name`、`message` 等 `LogRecord` 保留字段。
- 在负责消费失败的边界保留一次完整堆栈：异常处理块内使用 `logger.exception(...)` 或按恢复级别使用 `exc_info=True`。内部重新抛出的异常由上层处理时，避免逐层重复记录；后台任务必须有异常观测者。取消、用户打断和旧任务被拒绝分别表达，不能一律记为故障。异常堆栈和异常字符串也需检查是否包含敏感内容。
- 采用字段白名单，只记录定位问题需要的标识、阶段、状态和耗时。密码、API Key、JWT、Cookie、Authorization、带凭据或敏感查询参数的 URL、原始配置及请求/响应体不进入日志。默认不新增完整转写、提示词、模型回复或音频的记录；已有正文输出在修改对应路径时收敛为长度、状态等元数据。外部文字确需记录时限制长度并转义换行和控制字符，防止伪造日志行；日志提交问题前仍需人工脱敏。
- 音频帧、流式分片、轮询和连续重试按时间窗口或状态变化汇总，记录次数、丢弃量和恢复结果。避免在音频回调与事件循环热路径增加同步文件或网络日志 I/O。
- 修改日志基础设施时，队列须有界并规定满队列策略、丢弃计数、退出刷盘与超时；后台写入前捕获关联上下文，不能在监听线程读取原任务的 `contextvars`。多进程各自输出或通过独立收集器汇聚，不让多个进程直接轮转同一文件。

新增关键事件时，测试覆盖成功、最终失败及适用的取消/过期路径；使用 `caplog` 核对级别、关联字段、重复记录和合成敏感值未泄露。耗时测试验证字段及非负性，不绑定机器速度。日志不替代业务状态、持久化回执或测试断言。

结构化 JSONL、队列写入、指标、调用链和 Vector 属于后续按需改造；启用时需要明确生命周期、数据隔离及验证方式。当前不要求开发者部署额外观测服务。

## 数据、安全与兼容

- 配置通过已有白名单、锁和原子替换路径保存；新增配置同步 `config.example.py`。环境变量变化同步 `.env.example`，启动或备份行为变化同步操作指南。
- SQLite 身份映射升级需保持原编号；备份使用现有数据库备份接口，恢复前校验路径和内容，失败可恢复已有数据。
- AstrBot 写入保留事件创建时的目标范围和请求标识，以匹配回执和重试；切换房间后复查迟到结果。
- 浏览器 Cookie 会话、插件令牌和独立 API 按各自入口验证。新增路由核对认证、跨源写入限制和错误响应；外部输入不能直接成为文件路径、命令或 HTML。

## 原生前端

- 沿用两空格缩进、`camelCase` 和当前普通 script 加载方式。跨脚本接口在 `eslint.config.mjs` 显式登记，新增时核实定义位置与 `index.html` 加载顺序。
- 不可信文字使用 `textContent`；必须组合 HTML 时使用现有转义函数。异步刷新核对请求代次，避免旧响应覆盖新页面或用户草稿。
- 交互显示等待、失败和可重试状态；操作完成后恢复按钮，保留未保存输入。按钮具有可读名称，状态同时通过文字表达。
- 正式资源是唯一编辑源；`webui_preview.html` 由脚本生成。修改交互后验证正式页面和离线预览，覆盖移动端和失败路径。

## 开发与独立验证分工

行为变更与缺陷修复由开发 agent 自己执行 TDD：先固定需求、公开测试边界和预期行为，一次写一个能因目标错误失败的测试，实际运行并记录失败原因，再做最小实现、运行转绿，最后在测试保护下整理设计。已约定的接口与验收范围可直接沿用；需求歧义才需要澄清。文案、机械调整等不改变行为的任务使用适用检查，不制造形式化红测试。

开发 agent 对实现正确性负责。修改接口、增加等待点或后台任务前，列出受影响调用者，以及等待期间可能变化的状态、唯一拥有者和必须保持的约束；将适用约束落到行为验证。reviewer 的修复建议只作为参考，开发者须核实根因并选择方案。

涉及跨模块接口、并发、状态、持久化或同类回归时，由独立测试 agent 从需求和公开契约设计遗漏场景。它可提前设计反例，接口稳定后执行；不按实现照抄答案，也不只复查已有审查意见。开发与测试各用独立 worktree；测试 agent 不修改生产代码，新增测试经开发者集成后在最终快照验证。低风险局部修改可由开发者完成相关验证并说明适用范围。

独立验证选择真实相关的并发交错、失败、取消、重复及迟到场景，证明用户可观察行为；使用真实临时存储，模拟外部服务，不用替身替掉待验证能力。记录测试捕获原错误的证据、修复后结果和盲区。同一契约存在不同解读时先解决分歧，不让测试默默改变需求。

Reviewer 重点负责编码规范与架构，检查接口组合是否隐藏一致性协调、状态是否唯一归属，以及测试能否证明所声称行为；职责和报告见[审查分工](code-review.md#审查分工与交付)。测试数量、审查无意见或 CI 成功均不能单独证明正确性。

## 回归测试模板

测试描述可观察行为，使用 `tmp_path`、`monkeypatch`、合成凭据和模拟服务隔离；并发测试用事件控制顺序。修复竞态时至少构造“旧操作晚于新操作返回”的顺序，验证旧结果被拒绝。超时只作失败上限，不通过加长 sleep 保证顺序。

以下展示项目现有 `asyncio.run` 风格的取消测试结构；复制到具体模块时替换为真实对象的启动与关闭入口，断言其资源释放：

```python
import asyncio
from contextlib import suppress


def test_cancel_releases_resource() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        released = asyncio.Event()

        async def run() -> None:
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                released.set()

        task = asyncio.create_task(run())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        assert released.is_set()

    asyncio.run(scenario())
```

配置和协议改动覆盖无效输入及失败后状态；语音改动覆盖取消、重连、重复回执和旧会话。界面变更执行 `.github/workflows/ci.yml` 中四项浏览器检查。真实 Oopz 房间或模型服务验收单独记录，模拟通过不能代替实机验证。

## 本地工具与 CI

在已激活的虚拟环境中安装开发依赖，JavaScript 工具需要 Node.js 22.13+ 的 22.x 或 Node.js 24+；它们只用于开发和 CI，不改变 Python 应用的启动依赖：

```bash
python -m pip install -r requirements-dev.txt
npm ci
python -m pre_commit install
```

`pre_commit install` 是开发者自行选择的本地 Git 钩子安装步骤；配置文件随仓库提供。钩子运行当前环境的 Ruff、全项目 Pyright 与 npm 中锁定的 ESLint，只检查，不自动改写文件。运行 Pyright 前需按下文初始化本地配置。升级 Ruff 时同步开发依赖版本并复核新诊断。

```bash
python scripts/check_architecture.py
python -m ruff check .
npm run lint:js
python -m pyright
python scripts/build_webui_preview.py --check
python -m pytest -v -o faulthandler_timeout=60
python -m pre_commit run --all-files
```

首次本地类型检查需按操作指南初始化 `config.py`；CI 用示例配置，已有本地配置应保留。提交前运行完整 Python 套件；前端行为改动另按 CI 安装 Playwright 并运行浏览器脚本。PR 使用仓库模板填写实际验证结果和限制。

规则的范围与取舍：

| 检查 | 范围与目的 |
| --- | --- |
| 架构检查 | 固定一级目录、公开入口与依赖方向，拦截私有导入、退休命名空间和SDK越界 |
| Ruff | 保留现有规则，检查阻塞 HTTP、`time.sleep`、调试器与无诊断编号的类型忽略（PGH003）；F841/RUF059 检查部分未使用结果；日志检查见下文；显式传文件名也排除 SDK |
| Pyright | 全局 standard；`tool.pyright.strict` 内的解析模块强制参数、返回值及容器元素类型完整；本地钩子和 CI 均执行 |
| ESLint | 官方 recommended；覆盖正式 JS、浏览器测试和 lint 配置，区分 script、CommonJS 与 ES module |
| EditorConfig | 统一新编辑内容的编码、换行和缩进，不批量重排旧代码 |

ESLint 为普通 script 保留顶层声明，检查局部未使用变量；未使用参数和 catch 变量不强制清理。共享接口可在所属脚本中声明，同一文件的重复声明仍报错。当前保留两项兼容例外：`voice.js` 的包装函数重绑定 `refreshVoiceStatus`，该文件暂免 `no-func-assign`；`app.js` 将认证错误转换为 `AuthError`，该文件暂免 `preserve-caught-error`。重构对应边界时应复核并恢复规则。SDK WebView 的音量测试单独声明其页面内部变量。HTML 内联脚本、CSS 和生成预览不在本次 ESLint 范围，仍需浏览器检查。

当前没有统一重排历史代码的格式门禁。Ruff 的行宽是工具配置，未启用 `E501`，不代表已有每一行都强制不超过 100 字符。修复 lint 时优先调整代码；确有误报时仅抑制具体规则并解释原因，保持本地和 CI 一致。

上述检查不能证明业务不变量完整，也无法普遍发现“调用解析器后丢弃返回值”。`_ = parse(...)`、显式 `Any` 或带编号的忽略仍可能绕过约束，需按审查要求追踪。没有按函数名禁用所有布尔返回：纯查询与策略判定返回 `bool` 合理；要求解析的是需要把已确认信息交给后续业务的输入边界。

日志 lint 启用 `LOG004/LOG007/LOG014`（异常堆栈用法）、`G010/G101/G202`（弃用接口、保留字段冲突和冗余异常参数）、`PLE1205/PLE1206`（格式参数数量）。未全库启用 `G004` 等写法迁移规则；新增日志的参数化写法由审查核对。规则依赖静态识别 logger，不能保证包装器、动态消息、脱敏、关联字段或重复记录正确。

## Git 仓库工作流程

Agent 开工先检查 `git status --short`、当前分支和 diff，保留已有工作及本地配置。独立任务使用 `codex/<任务>` 短期分支；基线按实际发布流程选择，不能仅凭默认分支推断。并行实现优先使用独立 worktree。

- 每个提交形成完整且可验证的改变，包含相关测试与必要文档。大重构按实际可运行路径分批；机械移动与独立功能尽量分开。多个行为已耦合时先说明依赖，不能为了拆提交制造不可运行的中间状态。
- 使用明确路径或 `git add -p` 暂存，检查 `git diff --cached --stat` 和实际 diff；不盲目 `git add .`。排除凭据、运行数据、原始对话和无关产物。提交标题以动作说明变化，不强制提交前缀。
- 在开发虚拟环境中安装本地 pre-commit；每次克隆均需安装。`git commit` 自动检查架构、Python lint/类型及适用的 JS lint；它不运行 pytest。禁止为省事使用 `--no-verify` 或 `SKIP`。修复检查失败后重试。
- 提交前运行完整 Python 测试与适用检查；默认 pytest 仅收集 `tests/`，不得使用 `pytest .`。拆分提交应在各自提交快照验证，不能拿最终工作区通过代替中间提交可用。
- 尽早推送任务分支并建立 Draft PR。当前 CI 对 PR 和 `main/beta/dev` 的 push 触发；仅推送 `codex/*` 分支不触发该 push 工作流。PR 按模板描述范围、证据、未验证项及迁移剩余工作。
- 推送后核对远端 PR 的当前 head SHA、CI 结论及目标分支保护。工作流文件存在、CI 通过、必需状态检查阻止失败合并是三件事；权限不足或设置不可见须如实说明。
- 代码审查遵循 [自动审查与结果核验](code-review.md#自动审查与结果核验)：确认当前 head SHA 的实际完成证据，处理发现的问题后复查。只有请求评论、接收标记或 CI 成功时，交付状态仍写“审查未完成”。开发完成后核对 Draft 状态；设置和触发故障应记录原因与待办，不能将 PR 放置等待当作流程已结束。
- 合并目标应要求适用 CI 成功，并限制强推与删除；修改远端保护策略需有明确授权。不得自动合并、改写共享历史或删除其他人的分支。合并后的个人分支按团队约定清理。
- 发布和真实语音验收绑定提交 SHA；模拟测试不能替代双机器人验收。发现回归优先明确范围并使用可审查的修复或 revert。

依据：[GitHub flow](https://docs.github.com/en/get-started/using-github/github-flow)、[Google 小变更指南](https://google.github.io/eng-practices/review/developer/small-cls.html)、[分支保护](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)。
