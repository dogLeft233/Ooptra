# 系统维护

提供“应用配置并安全维护部署”的能力。公开接口见 [api.py](api.py)，不可变请求、计划和回执见 [models.py](models.py)，装配入口见 [工厂](__init__.py)。HTTP 只解析、认证和序列化；配置文件、任务台账、变更互斥、备份和父子交接由本模块拥有。全局边界见 [系统架构](../../ARCHITECTURE.md)。

## 能力导航

| 能力 | 隐藏的知识与状态 | 入口 |
| --- | --- | --- |
| [配置事务](_internal/configuration/README.md) | 文件版本、可信 Python 文档补丁、字段同步、幂等保存和应用回执 | `Configuration`、`ConfigurationRequest`、`configuration_transaction` |
| [备份与数据边界](_internal/backups/README.md) | 归档范围、SQLite 一致性副本、内容校验和停机恢复 | `BackupArchive`、`ValidatedRestore`、`StoppedDeployment` |
| [部署任务](_internal/deployment/README.md) | 类型计划、版本复核、单写者台账、持久交接、候选激活和回滚 | `Maintenance`、`RuntimeControl`、`Supervisor` |

## 配置使用与完成语义

输入适配器先按字段白名单解析为 `ConfigurationChange`；维护模块不从字典猜测编辑权限。`Configuration.apply(ConfigurationRequest)` 使用文件内容 SHA-256 作为预期版本，冲突抛出 `RevisionConflict`。同请求 ID 和相同载荷返回原回执，异载荷拒绝；`result` 可查询保存与应用状态。旧稀疏命令可以用 `apply_latest` 在锁内合并最新文件；浏览器编辑应传入读配置时获得的版本。

`configuration_transaction(root, operation)` 持有同一部署的跨进程变更许可，覆盖保存及组件应用，维护受理不能插队。调用者取消后仍观察已受理工作结束。`record_application` 将逐组件成功、失败及重启需求写入耐久回执，保存成功不代表组件全部应用。新版本已经提交时，禁止把旧请求重放为当前热应用。同 ID 的 partial 重试只执行失败组件和失败字段，保留成功项，不重写配置；已 applied 的重复请求直接返回原状态。

WebUI `/api/config` GET 返回版本；POST 可提供 `expected_revision` 和 `request_id`。GET 以 `request_id` 查询已保存/应用回执。语音快捷音量保存与全部字段保存使用相同文件补丁与部署许可。配置求值只适用于部署方可信的 Python 配置；不是不可信脚本沙箱。回执、repr 和日志不携带秘密值。

`load_voice_agent_settings` 显式解析配置、环境及 `.env.local`，导入不读取凭据。生成模块接收自己的只读设置视图；本模块不反向依赖行为、渠道或生成。已有环境覆盖仅在明确保存对应字段时同步。

`create_platform_configuration` 提供渠道所需的配置快照、登录来源、私钥读取和凭据保存端口；平台环境覆盖与凭据布局集中在本模块，渠道只接收快照。`create_credential_store` 保存前解析 RSA 私钥并从已确认的密钥结构重新序列化，不能把原始 PEM 尾随文本写成 Python。配置与私钥一起经原子文件工具替换，运行凭据同步仍在同一部署/配置锁内；即使逻辑配置路径是符号链接，也按部署根检查维护屏障。异步等待者取消后观察实际保存完成。

## 维护任务与生命周期

`plan(MaintenanceRequest)` 保存目标、部署/配置版本、有效期和影响，`execute(MaintenancePlan)` 复核后返回 `JobRef`；同计划重复执行关联原任务。当前任务与历史快照由同一记录写入口维护，`job` 和 `snapshot` 负责查询。兼容 HTTP 更新/恢复方法调用上述相同流程。

应用注入 `RuntimeControl`：配置应用、业务静默、请求退出及本地健康。准备结束后，维护请求 `quiesce`，完整成功才持久化 `HandoffRef` 并请求主循环退出；部分关闭失败保留 `closed/pending`，进入 `blocked_degraded`。应用核对当前进程已经持久化的交接版本和 token 后才通知退出；父监督确认自己创建的进程真正退出、PID/启动标记及交接匹配，才切换代码或恢复数据。

`deployment_access(root)` 从持久记录决定是否接受变更。候选切换/检查期间和未知、损坏、中断记录只提供查询与诊断；新进程不能凭新内存锁绕过屏障。父进程检查候选身份、解释器、启动标记及本地装配就绪；等待父提交的候选不提前连接业务平台。普通启动仍检查桥接监督器，Oopz/OneBot 离线仅为降级。候选成功后提交激活，失败先停止候选再回滚，不无限重试。

## 依赖与验证

维护只依赖公共机制和自己声明的管理端口；业务资源由应用用公开能力组合关闭。本模块不读取模块私有锁、SDK 或模型对象。入口仍保留 `launcher.py` 和 WebUI，任务状态不归入口所有。

运行 `.venv/bin/python -m pytest tests/operations tests/test_maintenance*.py tests/test_config_editor.py tests/test_auto_visit_config_editor.py -q`。公开契约测试覆盖 CAS、重复计划、保存/应用部分失败、回执发布失败后恢复、候选与损坏记录拒绝写入、取消保持互斥、部分静默禁止退出和停止身份复核。隔离真实子进程 rehearsal 覆盖候选等待提交及失败回滚；禁止为测试对当前部署执行更新或恢复。

文件内容与请求回执不是跨文件原子事务：写前耐久 pending，文件替换后发布 saved；崩溃窗口按目标摘要恢复为 saved 或 unknown。手工写文件及不使用本模块的外部程序不受协作锁保护。强制杀进程不等于完成静默。业务停机与本地维护验收不等同于真实发布/灾难恢复验收。
