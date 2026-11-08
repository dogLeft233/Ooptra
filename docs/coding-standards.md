# 代码要求与开发模板

适用于新增与修改的代码。先读 [架构地图](../ARCHITECTURE.md)，再定位状态拥有者和关联测试；公开工程资料及模板选型见 [依据记录](engineering-sources.md)。工具可执行的规则以 `pyproject.toml`、`eslint.config.mjs` 和 CI 为准，本文补充工具无法判断的约束。

## 修改边界

- 一个变更解决一个可验证的行为，代码、测试、示例配置和必要文档一起更新。大范围重排、格式化和包管理器迁移单独处理。
- 路由负责鉴权后的输入校验、调用和响应转换；房间操作交给 `VoiceAgent`，配置写入复用 `config_file_store`，维护任务复用现有监督流程。新增通用层前，先证明现有模块无法承载该职责。
- 持久化状态、观测快照和外部服务确认分别表达。入队、进程启动、WebUI 可访问都不能代替业务成功回执。
- 内置 `src/oopz_sdk/` 保持上游风格；兼容性改动应最小化，并通过对应测试验证。批量 lint 排除 SDK 不代表免于审查。

## Python 与接口

- 兼容 Python 3.10；新接口标注参数与返回值。外部输入的解析与类型要求见下一节。
- 可选语音依赖在对应能力入口加载，缺失时返回可理解的错误；纯桥接模式仍应能导入和启动。
- 常规业务捕获可预期的异常；边界层需要兜底时记录上下文并返回明确失败。错误日志保留堆栈，避免记录密码、Token、完整音频或含凭据的 URL。
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
from voice_agent.voice_control import VolumeCommand, parse_volume_command


def require_volume_command(raw: object) -> VolumeCommand:
    command = parse_volume_command(raw)
    if command is None:
        raise ValueError("此操作需要音量指令")
    return command  # 调用方继续传递 command，而不是 raw。
```

`src/voice_agent/voice_control.py` 是首批严格类型检查的解析模块：动作使用 `Literal`，构造器检查音量范围和退房/音量互斥关系。现有单一 `VolumeCommand` 仍用运行时约束表达 action/value 关系；类型检查尚不能从 `action == "set"` 单独推导 value 非空。旧后端中的字典协议仍需渐进迁移，不把该试点视为全库完成。

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
| Ruff | 保留现有规则，检查阻塞 HTTP、`time.sleep`、调试器与无诊断编号的类型忽略（PGH003）；F841/RUF059 检查部分未使用结果；显式传文件名也排除 SDK |
| Pyright | 全局 standard；`tool.pyright.strict` 内的解析模块强制参数、返回值及容器元素类型完整；本地钩子和 CI 均执行 |
| ESLint | 官方 recommended；覆盖正式 JS、浏览器测试和 lint 配置，区分 script、CommonJS 与 ES module |
| EditorConfig | 统一新编辑内容的编码、换行和缩进，不批量重排旧代码 |

ESLint 为普通 script 保留顶层声明，检查局部未使用变量；未使用参数和 catch 变量不强制清理。共享接口可在所属脚本中声明，同一文件的重复声明仍报错。当前保留两项兼容例外：`voice.js` 的包装函数重绑定 `refreshVoiceStatus`，该文件暂免 `no-func-assign`；`app.js` 将认证错误转换为 `AuthError`，该文件暂免 `preserve-caught-error`。重构对应边界时应复核并恢复规则。SDK WebView 的音量测试单独声明其页面内部变量。HTML 内联脚本、CSS 和生成预览不在本次 ESLint 范围，仍需浏览器检查。

当前没有统一重排历史代码的格式门禁。Ruff 的行宽是工具配置，未启用 `E501`，不代表已有每一行都强制不超过 100 字符。修复 lint 时优先调整代码；确有误报时仅抑制具体规则并解释原因，保持本地和 CI 一致。

上述检查不能证明业务不变量完整，也无法普遍发现“调用解析器后丢弃返回值”。`_ = parse(...)`、显式 `Any` 或带编号的忽略仍可能绕过约束，需按审查要求追踪。没有按函数名禁用所有布尔返回：纯查询与策略判定返回 `bool` 合理；要求解析的是需要把已确认信息交给后续业务的输入边界。
