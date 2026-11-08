# 架构地图

本文是贡献者的代码地图，描述当前实现的职责、运行路径和状态边界。代码实体使用名称作为搜索锚点；安装与操作步骤见 [README](README.md)。

## 系统边界

Ooptra 将 Oopz 的文字事件与动作接入 OneBot v11 框架，并在 Oopz 语音房运行模型对话。Oopz 服务负责账号、消息与房间；AstrBot 等外部框架负责文字回复；Gemini Live、MiMo 等外部服务负责语音模型能力。配套 `astrbot_plugin_ooptra` 运行在 AstrBot 中，负责访问其人格、会话及长期记忆插件，本仓库不实现这些外部存储。

正常运行时，控制台、文字桥接、语音编排和自动串门共享 Python 应用进程，以 asyncio 任务协作；语音另依赖 SDK 的浏览器传输。通过 `launcher.py` 启动时，维护监督器是应用的父进程，可在应用停止后切换版本、恢复数据并重新启动。

## 代码地图

| 位置与关键实体 | 职责与修改入口 |
| --- | --- |
| `main.py` 的 `run` | 装配控制台、桥接和语音运行时，管理启动与退出顺序 |
| `src/bridge/` 的 `BridgeController`、`BridgeState` | 监督 SDK 生命周期，连接 OneBot 服务，汇总连接和流量状态 |
| `src/oopz/` | 将项目配置、凭据、代理及名称解析接到 SDK |
| `src/onebot_v11/` 的 `OneBotV11Supplement` | 项目协议配置、数据库迁移，以及 SDK 缺失动作和心跳的补充 |
| `src/oopz_sdk/` | 随仓库分发的第三方 SDK：REST/WS、事件、OneBot adapter、身份与消息映射、浏览器语音传输 |
| `src/voice_agent/` 的 `VoiceRuntime`、`VoiceAgent` | 装配后端/API/串门调度；拥有房间会话、回复、播放与取消状态 |
| `src/voice_agent/backends/` | 模型后端契约与 Gemini Live、MiMo、阿里语音 + DeepSeek 级联实现 |
| `src/webui/` 的 `WebUIConsole` | aiohttp 控制台、认证、配置编辑、语音路由、日志和维护入口；静态资源在 `assets/` 子目录 |
| `src/core/` | 路径、日志、版本、代理及配置文件写入等共享机制 |

`tests/` 包含 Python 回归测试，`tests/browser/` 验证实际页面交互。根目录 `webui_preview.html` 由 `scripts/build_webui_preview.py` 从正式资源和示例配置生成，带隔离演示接口；修改界面时从正式资源更新预览。

## 主要运行路径

### 启动与文字桥接

`main` 应用环境覆盖并校验配置；`run` 先启动控制台，再启动 `BridgeController`，最后启动语音运行时。这样桥接凭据或对端故障时，控制台仍可用于恢复。控制台已响应与应用完成启动是不同状态，由 `bootstrap_ready` 区分。

`BridgeController` 组装 `OopzBot`、SDK OneBot adapter、`BridgeOneBotV11Server` 与 `OneBotV11Supplement`。Oopz 事件经 adapter 转成 OneBot 事件，通过反向 WebSocket 交给外部框架；外部动作沿同一协议入口交回 adapter，再调用 Oopz 能力。`SdkBridgeShim` 提供项目补充动作所需的 SDK 访问接口。`BridgeState` 保存观测快照，不替代 adapter 的身份与消息数据库。

重启请求通过监督任务串行处理，避免启动和拆除交错。桥接重建也会重新绑定语音运行时使用的 bot；修改连接恢复时，应同时检查语音传输的失效与回收。

### 语音与自动串门

WebUI 语音接口和可选独立 `VoiceApiServer` 面向同一个 `VoiceRuntime`。运行时拥有一个 `VoiceAgent` 与一个 `AutoVisitController`，手动控制、模型控制和自动串门最终共享房间操作。

音频经过 SDK 浏览器传输与 `VoiceDuplex` 进入 Agent。Gemini Live 持续收发音频；MiMo 与阿里语音 + DeepSeek 级联经 VAD 断句后执行 ASR → LLM → TTS。`AliyunDeepSeekBackend` 复用级联策略，通过 DeepSeek SSE 分句驱动阿里 WebSocket TTS，再由 Agent 边收边推送音频；ASR 仍在本地断句后发送。`StreamingUtterance` 描述流式回调契约，Agent 拥有播放、打断和房间代次校验。回复策略、抢话与输出控制参与发送和播放。后端接口与实际实现需一并检查，工厂存在某个名称并不表示对应服务已完整接入。

Agent 用操作锁、操作代次和访问标识协调进退房及迟到任务。串门控制器维护独立调度状态，但通过 Agent 操作房间。修改自动退房时，需要保留其对当前房间与操作归属的复查，避免旧调度任务影响手动会话。

### AstrBot 人格与记忆同步

`ContextBridge` 通过 HTTP 层承载的轮询/回执 RPC 与外部插件协作，这条通路独立于文字 OneBot WebSocket。进房时准备上下文，模型可请求查询或显式写入记忆；语音轮次也可排队交给插件处理。

请求携带 `request_id`、`session_id`、房间和来源范围。待写事件保留创建时的范围；切换房间会清理当前上下文及相关待响应请求，而持久化待写队列继续按原目标处理。插件回执决定成功、失败或重试；入队本身不代表长期记忆写入成功。具体契约见 [AstrBot 同步说明](docs/astrbot-context-sync.md)。

### 更新、备份与恢复

WebUI 维护模块准备任务，并在 `data/maintenance/` 记录任务和运行环境。`launcher.py` 进入 `maintenance_worker`，监督应用子进程；符合维护任务条件的退出触发停止后的快照、版本/数据切换、重启和健康检查，失败时尝试回滚。

`BackupStore` 管理配置和用户数据归档，对 SQLite 使用数据库备份接口。健康检查区分进程身份、启动就绪与外部连接：Oopz/OneBot 暂时断连属于降级，不单独触发版本回滚。直接运行 `main.py` 不具备这一父进程维护流程。

## 状态所有权与横切边界

- **本地持久化**：配置及账号密钥分别位于 `config.py`、`private_key.py`；阿里与 DeepSeek 模型密钥由后端设置从进程环境或被忽略的 `.env.local` 读取，不进入 WebUI 配置。OneBot 身份和消息映射默认在 `data/onebot_v11.sqlite3`。保留映射是升级连续性的要求。
- **两类记忆**：`MemoryStore` 管理本地 JSONL 语音记录；`ContextBridge` 的待写 outbox 位于记忆文件旁。AstrBot 长期记忆由外部插件管理，清空本地记录不会调用外部记忆删除。
- **配置应用**：WebUI 配置编辑复用 `src/core/config_file_store.py` 的写入锁、暂存替换和失败回滚；运行时另需应用变化。`VoiceRuntime.reload_settings` 更新共享设置并刷新派生对象，独立语音监听地址/端口变化仍要求重启。
- **跨线程音频**：`VoiceDuplex` 将回调调度到所属事件循环，再写入有界队列；关闭后的迟到帧和队列满时的帧会被丢弃。修改音频入口时保持该交接边界。
- **认证**：`ConsoleAuth` 区分浏览器会话 Cookie 与插件令牌，控制台中间件检查受保护路由及浏览器写操作来源。登出浏览器不等于撤销插件凭据；独立 API 的认证也需单独检查。

这些边界主要由对象装配、接口和回归测试维持，并非独立包的依赖隔离。已有测试覆盖桥接重启、duplex 重绑定、上下文过期回执/持久化重试和维护回滚；这不代表真实 Oopz 房间或外部模型服务已通过运行验收。

## 导航与维护

修改某条路径时，先从对应装配实体追踪到状态拥有者，再检查相应 `tests/test_*.py`；界面变化还需检查浏览器测试和预览生成。配置与部署细节见 [配置与维护](docs/operations.md)，语音操作见 [语音指南](docs/voice-guide.md)。

入口、模块职责、状态归属、进程边界或主要数据通路发生变化时更新本文；内部实现替换且边界不变时无需逐提交同步。以当前代码和测试核对架构断言，历史审计记录保留其原有时间范围。
