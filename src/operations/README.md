# 系统维护

当前状态：**部分实现**。已迁入语音配置加载/就地应用、全部字段的配置保存事务、共用 AST 文档补丁，以及现有备份、清理、更新传输、维护任务和受管父进程。版本化配置事务、具名计划与跨进程 CAS 仍为目标契约。已实现能力以 [公开接口](api.py)、[公开类型](models.py) 和 [工厂及文档操作](__init__.py) 为准，以下“待实现”签名不可直接调用。全局规则见 [系统架构](../../ARCHITECTURE.md)，目录约束见 [代码要求](../../docs/coding-standards.md#模块目录与接口导航)。

## 职责与非职责

提供“可审计地应用配置并安全维护部署”的能力，隐藏配置文件、备份布局、更新预检、任务持久化、父子进程交接与回滚。HTTP 认证/序列化仍归 WebUI 入口；运行中的真实房间、策略配额与模型会话分别归各业务模块。维护不借 WebUI 或进程对象后门直接修改业务私有状态。

## 内部能力地图

| 能力 | 隐藏的知识与状态 | 当前迁移入口 |
| --- | --- | --- |
| [配置事务](_internal/configuration/README.md) | 已实现归一化变更快照、同进程全字段保存/运行字典同步和共用文档补丁；文件版本/组件应用回执仍待迁入 | `api.Configuration`、`models.OutputVolume`、`_internal/configuration/` |
| [备份与数据边界](_internal/backups/README.md) | 归档清单、校验结果、停机恢复、受保护集合与清理计划 | `api.BackupArchive`、`api.StorageScanner`、`_internal/backups/` |
| [部署任务与受管执行](_internal/deployment/README.md) | 计划、单写者任务状态、父子交接、就绪检查和回滚 | `api.Maintenance`、`api.UpdateTransport`、`api.Supervisor`、`_internal/deployment/` |

父进程与应用进程是同一维护模块的不同运行角色，不要求搬进同一运行进程。界面状态由任务查询生成，不另存一份维护状态机。

## 已实现的配置入口

`create_configuration(path=..., runtime_config=...)` 返回 `Configuration` 协议。调用方将原始音量解析为不可变 `OutputVolume`，随后调用 `save_output_volume`；磁盘与运行配置字典同步后返回 `VolumeSaveReceipt`，播放增益由语音输出能力继续应用。此切片不把保存称为运行组件已热应用，也尚无版本检查、请求台账或跨进程互斥。

`patch_config_text` 与 `validate_config_text` 是兼容迁移所用的公开文档操作，不授予字段写入权限。前者只转换正文，后者沿用旧编辑器的求值校验，会执行部署方可信的 Python 配置，不能用于不可信上传脚本；字段白名单先由调用方解析。WebUI 编辑器保留界面元数据、字段准入和归一化；磁盘事务与运行字典同步统一调用 `Configuration.save_fields`，原编辑器的 AST 转调与同步实现已移除。改变音量合法范围只需修改 `OutputVolume`；改变 AST 补丁算法只需修改本模块。

`load_voice_agent_settings` 显式读取部署配置、环境变量及 `.env.local`，返回 `models.VoiceAgentSettings`、`VoiceApiSettings`；导入模块不读取凭据。默认值、环境优先级、共享 WebUI token 和历史限值兼容规则统一归本模块。`apply_settings` 就地更新已解析设置并报告变化字段，组件重建仍由应用装配协调。语音模型/API配置的密钥和 token 不进入 repr。调用方可注入配置、环境及本地环境文件路径用于隔离加载。原 `voice_agent/settings.py` 已移除；对话生成使用自己的设置端口，不反向依赖本模块。

`ConfigurationChange.from_normalized` 接收输入边界已批准的字段，冻结嵌套字典/列表并保留来源名称，保存时使用其独立快照。`save_fields` 在一次共享文件锁内读取磁盘、合并必要的稀疏更新、校验可信配置、原子替换并同步所保存的运行字段；其他环境覆盖不被写回或清除。音量保存也使用同一事务。自动串门的纯合并规则由输入边界注入，维护模块不反向导入行为模块；规则在锁内针对最新磁盘值执行。回执只列字段名，不包含秘密值，不代表业务组件已热应用。

`VoiceRuntime` 已通过注入的配置能力保存音量，不再导入 WebUI。全部字段保存和维护文件操作已迁入本模块；业务组件热应用仍由运行装配协调。

## 已实现的维护入口

应用通过 `create_maintenance(root, shutdown=..., runtime_status=..., bootstrap_ready=...)` 装配维护门面。WebUI 的 `maintenance.py` 仅保留 JSON/Origin 校验、HTTP 状态和文件响应；任务状态、互斥、更新检查、清理预览和准备线程都由本模块持有。网络测试也通过维护门面调用，不在路由另建网络策略。

`launcher.py` 通过 `create_supervisor().supervise(root)` 监督自己创建的子进程；父进程在旧子进程退出后执行切换、健康检查与回滚。平台连接离线只表示降级，启动标记和解释器身份不匹配则不接受候选。现有兼容 JSON 阶段和归档格式保持不变。

`create_backup_store`、`create_storage_scanner`、`create_update_transport` 是可独立使用的维护子能力；操作/失败/阻塞约束以 [公开接口](api.py) 为准。默认工厂不发起网络或进程操作。`create_maintenance_platform` 负责提供时钟、Git/网络、存储及健康端口，测试或其他运行环境可以替换端口，不能借端口维护另一份任务状态。

更新请求先解析为不可变 `UpdateTarget`，保留确认后的渠道及完整提交供准备流程使用。`NetworkSettings` 拥有代理/镜像校验及脱敏 repr。等待备份、删除和清理的请求被取消后，已启动线程仍被观察至完成，期间不释放维护互斥，`close` 也等待这些工作。取消不意味着磁盘操作没有发生。

现有维护仍使用字典回执、单个 JSON job 文件及应用内互斥，没有实现下面的版本化计划/CAS，也尚未把配置提交和任务变更合并为一个 mutation gate。不得把迁移目录视为这些目标约束已全部实现。

## 待实现公开契约

- `apply_config(change: ConfigChange) -> ConfigApplyReceipt`：带预期配置版本，解析成功后才应用；返回持久化版本、各项应用状态、需重启项及失败项。跨模块热更新不宣称原子事务；已保存但应用失败必须可查询/重试，不能伪报全部成功。
- `plan(request: MaintenanceRequest) -> MaintenancePlan`：返回已检查目标、前置条件、有效期、影响与保护范围；不是授权无限制 shell 命令。
- `execute(plan: MaintenancePlanRef) -> JobRef`：复查计划绑定的版本、目标与环境；同计划重复请求关联同任务，冲突任务拒绝。返回仅表示已受理，效果通过 `job` 查询。
- `config_result(request_id: ConfigRequestId) -> ConfigApplyState`：查询已提交配置请求的 pending/应用回执/未知状态；等待者取消不把磁盘保存事实撤销。
- `job(id: JobRef) -> JobSnapshot`、`snapshot() -> SystemSnapshot`：区分预检、准备、交接、重启、就绪、成功、失败和回滚结果；应用断开不意味着维护失败，父进程继续记录任务。

`operations.models` 拥有配置变更、计划与任务类型。`operations.api` 声明窄管理端口（应用配置、停止业务、启动就绪/降级查询）；`app` 用其他模块公开能力实现并注入。维护不导入其他模块实现，其他业务不反向导入维护；配置由装配转换成各模块自己的配置类型。

## 生命周期与故障

正常停机先停新请求与行为调度，撤销正在生成和执行的动作，渠道释放会话，持久化上下文投递，再交父进程维护；具体资源关闭由拥有者完成。任务状态存储独立于 HTTP 连接，请求超时不能创建重复 worker。应用未就绪与外部连接降级分开，暂时 Oopz/AstrBot 离线不作为版本回滚条件。重启后恢复/核对中断任务，不把未知任务当成功。

保留既有凭据、数据和 `data/onebot_v11.sqlite3`；备份/恢复/删除只操作已解析且受限的目标。失败记录恢复结果与可操作提示，不记录凭据或原始命令敏感输出。操作规则沿用 [配置与维护指南](../../docs/operations.md)，架构迁移不修改用户更新源或现有数据。

## 依赖与验证

静态依赖仅公共技术机制，运行时通过本模块管理端口协调。存储和 OS 适配留在本模块；WebUI 调用其公开 API。输出音量保存已消除 `VoiceRuntime` 对 `webui.config_editor` 的反向导入；现有配置保存与部署入口均已归属本模块，HTTP 表单准入和组件热应用仍位于各自入口/装配。

配置切片通过 `tests/operations/test_configuration.py` 验证保存保留无关正文/环境覆盖、无效输入、磁盘失败及动态目标拒绝，原 WebUI/音量/配置重载测试继续回归。运行：`.venv/bin/python -m pytest tests/operations tests/test_config_editor.py tests/test_auto_visit_config_editor.py tests/test_voice_output_controls.py -q`。

新增公开维护行为见 `tests/operations/test_maintenance_contract.py`：更新目标解析、真实归档验证/恢复、任务查询与取消期间的互斥。原有维护回归通过测试平台端口适配继续验证，适配不导入内部模块；真实子进程切换/回滚仍由隔离 rehearsal 守住。运行：`.venv/bin/python -m pytest tests/operations tests/test_maintenance*.py -q`。

现有证据：`tests/test_config_editor.py`、`tests/test_maintenance_deadlines.py`、`tests/test_maintenance_rehearsal.py`、`tests/test_maintenance_health.py`、`tests/test_maintenance_payload_boundaries.py`。迁入后 `tests/operations/` 验证版本冲突、保存/应用部分失败、重复执行、进程中断恢复和回滚；用隔离目录/模拟进程，禁止为架构验证对当前仓库执行真实更新或恢复。

## 内部依赖与装配

目标设计：部署任务 → 备份能力及配置只读检查；配置和备份不反向依赖部署。公开外观将持有一个 operations 内部变更许可，串行配置提交/应用和维护受理；它结合持久化 active-job 和启动角色决定是否允许变更：候选 checking 期间只开放查询/health，不能由新进程内存锁绕过维护屏障。它不复制任务状态或配置规则。`core` 只保留文件替换/路径等机制。

管理端口的目标方法为 `apply_config(revision, parsed_config) -> RuntimeApplyReport`、`quiesce(job_id, deadline) -> QuiesceResult`、`request_exit(handoff_ref) -> ExitResult`、`health() -> RuntimeHealth`。`app` 将各模块公共能力组合实现，配置类型归 operations，业务配置映射由装配完成；不允许端口返回运行时/SDK 可变对象。quiesce 必须报告已关闭/未关闭资源；部分失败保留 degraded/blocked，不能声称旧服务继续正常运行。成功后持久化交接引用并通过 request_exit 通知主循环退出，父进程实际确认旧子进程退出后才切换。

## 实现与阅读顺序

迁入后依次阅读 `api.py` → `models.py` → 所需 `_internal/` 实现。上述子模块已以 README 落实详细设计，Python 文件按真实迁移切片创建，不为其余目标能力预建空 Protocol 或默认实现。跨模块只使用公开契约，工厂由应用装配，禁止返回内部可变对象。迁移批次与验收见 [重构准备计划](../../docs/superpowers/plans/2026-11-08-module-refactor-readiness.md)。
