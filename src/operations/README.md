# 系统维护

当前状态：**目标设计，业务代码尚未迁入**。以下签名为待实现契约，不可直接导入；首条真实路径迁入时创建 `api.py`、`models.py` 和工厂，之后以代码契约为准，删除本文重复签名。全局规则见 [系统架构](../../ARCHITECTURE.md)，目录约束见 [代码要求](../../docs/coding-standards.md#模块目录与接口导航)。

## 职责与非职责

提供“可审计地应用配置并安全维护部署”的能力，隐藏配置文件、备份布局、更新预检、任务持久化、父子进程交接与回滚。HTTP 认证/序列化仍归 WebUI 入口；运行中的真实房间、策略配额与模型会话分别归各业务模块。维护不借 WebUI 或进程对象后门直接修改业务私有状态。

## 内部能力地图

| 能力 | 隐藏的知识与状态 | 当前迁移入口 |
| --- | --- | --- |
| [配置事务](_internal/configuration/README.md) | 解析、文件版本、提交/热应用回执、部分失败与重试 | `webui/config_editor.py`、`core/config_file_store.py`、`VoiceRuntime.reload_settings` |
| [备份与数据边界](_internal/backups/README.md) | 归档清单、校验结果、停机恢复、受保护集合与清理计划 | `webui/maintenance_storage.py`、`maintenance_cleanup.py` |
| [部署任务与受管执行](_internal/deployment/README.md) | 计划、单写者任务状态、父子交接、就绪检查和回滚 | `MaintenanceService`、`launcher.py`、`maintenance_worker.py` |

父进程与应用进程是同一维护模块的不同运行角色，不要求搬进同一运行进程。界面状态由任务查询生成，不另存一份维护状态机。

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

静态依赖仅公共技术机制，运行时通过本模块管理端口协调。存储和 OS 适配留在本模块；WebUI 调用其公开 API。旧配置写入迁入后，`VoiceRuntime` 不再反向导入 `webui.config_editor`。

现有证据：`tests/test_config_editor.py`、`tests/test_maintenance_deadlines.py`、`tests/test_maintenance_rehearsal.py`、`tests/test_maintenance_health.py`、`tests/test_maintenance_payload_boundaries.py`。迁入后 `tests/operations/` 验证版本冲突、保存/应用部分失败、重复执行、进程中断恢复和回滚；用隔离目录/模拟进程，禁止为架构验证对当前仓库执行真实更新或恢复。

## 内部依赖与装配

部署任务 → 备份能力及配置只读检查；配置和备份不反向依赖部署。公开外观持有一个 operations 内部变更许可，串行配置提交/应用和维护受理；它结合持久化 active-job 和启动角色决定是否允许变更：候选 checking 期间只开放查询/health，不能由新进程内存锁绕过维护屏障。它不复制任务状态或配置规则。`core` 只保留文件替换/路径等机制。

管理端口的目标方法为 `apply_config(revision, parsed_config) -> RuntimeApplyReport`、`quiesce(job_id, deadline) -> QuiesceResult`、`request_exit(handoff_ref) -> ExitResult`、`health() -> RuntimeHealth`。`app` 将各模块公共能力组合实现，配置类型归 operations，业务配置映射由装配完成；不允许端口返回运行时/SDK 可变对象。quiesce 必须报告已关闭/未关闭资源；部分失败保留 degraded/blocked，不能声称旧服务继续正常运行。成功后持久化交接引用并通过 request_exit 通知主循环退出，父进程实际确认旧子进程退出后才切换。

## 实现与阅读顺序

迁入后依次阅读 `api.py` → `models.py` → 所需 `_internal/` 实现。上述子模块已以 README 落实详细设计，Python 文件仍按真实迁移切片创建，暂不创建空 Protocol 或默认实现。跨模块只使用公开契约，工厂由应用装配，禁止返回内部可变对象。迁移批次与验收见 [重构准备计划](../../docs/superpowers/plans/2026-11-08-module-refactor-readiness.md)。
