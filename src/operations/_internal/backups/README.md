# 备份与数据边界

状态：现有归档和清理实现已迁入本目录的 `storage.py`、`cleanup.py`。公开入口见 [系统维护](../../README.md) 及 `api.BackupArchive`、`api.StorageScanner`；保留现有归档格式和用户数据布局。以下具名验证引用、停机凭据是后续目标契约，尚未实现。

## 独立职责

集中隐藏“哪些数据可以备份、如何取得一致副本、如何校验和恢复、哪些文件允许删除”。备份可以不更新代码，恢复也不要求生成模块参与；因此独立于 [部署任务](../deployment/README.md)。归档校验与恢复同属一个子模块，调用者不得先自行解包再提供任意文件路径。

## 内部契约与状态

```text
Backups.create(request: BackupRequest) -> BackupRef | BackupFailure
Backups.inspect(id: BackupId) -> BackupManifest | InvalidBackup
Backups.stage_restore(id, destination: OwnedStage) -> ValidatedRestore | InvalidBackup
Backups.restore(restore: ValidatedRestore, stopped: StoppedDeployment) -> RestoreResult
Backups.preview_cleanup(protected: BackupReferences) -> CleanupPlan
Backups.apply_cleanup(plan: CleanupPlan, protected: BackupReferences) -> CleanupOutcome
```

以上是内部任务式操作，不要求每个方法各建一个类。`Backups` 持有部署根和归档目录权限；部署任务传已验证的引用，不把 SDK/运行时对象带进来。

| 类型 | 需要保留的知识 |
| --- | --- |
| `BackupId` / `BackupRef` | 受约束标识、清单版本、创建时间；外部字符串先解析，不是任意路径 |
| `BackupManifest` | 受管相对路径、尺寸/摘要、归档格式与数据库一致性说明；配置内容、密钥不放公开响应 |
| `ValidatedRestore` | 已校验备份 ID/指纹、私有 staging 句柄、预期文件集；只能由本模块产生；使用前复查指纹，不能恢复被替换归档 |
| `StoppedDeployment` | 部署任务确认的旧进程身份、停机事实及数据库关闭状态；不是调用者可随便传的 `True` |
| `BackupReferences` | 当前任务及回滚保留的备份 ID，来自任务仓库的只读快照；不可读取时禁止删除 |
| `CleanupPlan` | token、有效期、允许删除对象及其指纹、受保护集合版本；token 不是通用删除授权 |

上下文迁为独立 SQLite 时，归档目录清单必须包含由可信配置派生的新库路径及格式标识，在线使用 SQLite backup，不只复制 .sqlite3 主文件而遗漏 WAL 中已提交记录。导入旧记忆文件前后均保留可识别的备份版本；旧版本代码不能直接恢复并读取不支持的新 schema，降级需用该批次验证过的导出/回退方案，失败时停止恢复而非丢记录。此要求随上下文迁移落地，不表示现有 BackupStore 已支持新库。

本模块独占 staging/归档创建和数据恢复写入；数据库仍由其业务模块管理。在线备份通过 SQLite backup 等原有一致性接口；最终切换/恢复前在数据库关闭后再作安全快照。不能把跨多个在线文件的复制称为同一时刻全库事务。

## 安全与故障收尾

- 归档路径、大小、重复项、符号链接及展开目标均在本模块校验；验证结果保留为对象供恢复使用，不能校验后丢弃再用原始文件名。
- 备份写临时归档，完整写入并校验后发布；失败删除自己创建的暂存项，不删除旧备份。取消发生在线程复制期间，任务仍需观测实际完成才能释放维护许可。
- 恢复在 `StoppedDeployment` 有效时进行；遇到部分替换失败，返回已知变更范围并交部署子模块回滚。备份模块不能自行拉起进程或切换 Git 版本。
- 删除前重新读取任务引用和候选指纹；引用记录损坏、任务进行中、指纹变化或计划过期均拒绝。清理是已有部署根内的允许项，不能由模型/HTTP 提供任意删除路径。
- 维护用的安全快照在任务结束前受保护；保留失败任务所需回滚证据，不能按空间清理误删。归档权限延续原有敏感文件约束，响应和日志不泄漏正文。

## 迁移与测试

`BackupStore` 和清理逻辑已迁入，保留现有归档版本、分页与删除兼容响应；部署准备仍使用原有校验返回值，具名验证对象有待后续实现。不能一边保留旧恢复入口绕过保护，一边宣称数据边界已统一。

现有证据：`tests/test_maintenance_backups.py`、`tests/test_maintenance_archive_boundaries.py`、`tests/test_maintenance_backup_delete.py`、`tests/test_maintenance_storage_cleanup.py`。需补受验证引用被替换、恢复前未停机、在线/离线快照语义、任务引用损坏和清理重检的公开行为测试；使用隔离目录和合成凭据，不恢复当前仓库。

公开行为验证见 `tests/operations/test_maintenance_contract.py`。现有 HTTP 备份删除/清理回归与真实 SQLite 归档测试继续使用同一实现；取消备份等待者后，维护门面持续拥有写线程及互斥，避免第二个变更和未结束 I/O 交错。
