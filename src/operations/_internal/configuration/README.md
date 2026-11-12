# 配置事务

状态：**现有保存能力已迁入，版本化事务待实现**。`settings.py` 拥有语音配置加载、环境优先级和就地应用规则，公开设置类型归 `operations.models`；`service.py` 拥有全部字段保存与运行字典同步，`document.py` 拥有共用 AST 补丁/文档校验。公开入口见 [系统维护](../../README.md) 的 `Configuration.save_fields` 与 `save_output_volume`。文件写入复用 `core.config_file_store` 的同进程锁。WebUI 保留表单 schema、字段准入与归一化；不再读取/提交配置文件或执行运行同步。`VoiceRuntime` 继续用注入的 `Configuration` 保存音量。下文版本指纹、请求回执和组件热应用协调仍为目标设计。

## 边界与选择

配置保存和热应用放在同一任务能力内，隐藏字段合法性、原文件补丁、版本检查、环境覆盖、提交和重试。若拆成 HTTP 校验、存储保存、运行时补丁三个公开步骤，所有调用者都必须了解失败顺序，因此不采用。磁盘格式编辑与运行时映射是内部适配函数；没有独立状态，不再建两层 service/repository。

## 状态与类型

| 目标类型 | 最小字段及不变量 |
| --- | --- |
| `ConfigRevision` | 已管理文件集的内容指纹；只在同一部署根比较，不用文件 mtime 当版本；不对外泄漏密钥或文件正文 |
| `ParsedConfigChange` | request ID、expected revision、具名字段变更；布尔/整数等严格区分，未知或互斥字段在解析时拒绝；合法对象后续复用 |
| `ConfigDocument`（内部） | 原始文本、解析后的受管字段、文件指纹；保留用户注释/非相关值，不能把运行时环境覆盖无意写回磁盘 |
| `ConfigCommit`（内部） | request ID、before/after revision、变更字段路径、已持久化文件；不存秘密值的日志副本 |
| `RuntimeApplyReport` | 按目标组件的 applied revision、failed/restart-required/not-applicable；可含脱敏错误码，不用单个 `ok` 隐藏部分成功 |
| `ConfigApplyReceipt`（公开） | request ID、磁盘版本、运行时各组件报告；pending/partially-applied/applied/restart-required/persistence-uncertain 明确可区分 |

唯一拥有者 `Configuration` 持有当前磁盘版本的观测、同进程写入互斥、运行时应用回执。真正运行设置仍由各业务模块拥有；这里的 applied revision 是回执，不是第二份可修改运行配置。版本在每次提交前重新读取，不以缓存覆盖外部编辑。

## 内部接口草案

```text
Configuration.apply(change: ParsedConfigChange) -> ConfigApplyReceipt
Configuration.inspect() -> ConfigInspection
Configuration.retry_apply(request_id, expected_revision) -> ConfigApplyReceipt
ConfigFiles.read() -> ConfigDocument
ConfigFiles.commit(expected_revision, patched_document) -> ConfigCommit | Conflict | PersistenceUncertain
```

`ConfigFiles` 是私有磁盘适配，不必为了这些方法创建基类；有测试替换需求时用结构协议。`Configuration` 只接受 operations API 声明的 `RuntimeControl.apply_config` 端口，不导入 `VoiceRuntime`、WebUI 或各模块实现。应用装配把配置映射成目标模块配置；端口返回具名结果，而非可执行任意回调。

## 提交、取消与恢复

现有同步保存由输入任务拥有：WebUI 将“文件保存、共享 token 同步、必要热应用和已授权重连”放入受控任务，取消 HTTP 等待者仍会观察任务实际结束，然后传播取消；并发保存连同应用串行。WebUI 停止时关闭保存准入、等待在途任务，再释放维护/HTTP资源；应用入口必须在关闭语音等业务资源前等待控制台收尾。音量命令同样观察其保存线程结束。上述不提供崩溃恢复，也不为阻塞写入虚构强制取消；以下版本化阶段仍为目标设计。

```text
parsed → acquired mutation gate → checked revision → staged → committed
  → applying → applied / partially-applied / restart-required
校验/版本冲突 → rejected（不写）
提交失败且回滚确认 → failed（旧版本）
无法确认磁盘状态 → persistence-uncertain（停止继续热应用）
```

1. 模块写入口先取得 operations 的维护互斥许可，再在线程中执行读/改/提交；文件事务使用既有进程内 RLock。跨进程写入必须先证明部署独占，不能把进程内锁写成跨进程保证。
2. 版本比较与补丁提交处于同一次本地写事务，落地测试模拟外部改动；不能承诺可阻止不遵守锁的外部编辑，提交后再次核实指纹，冲突报不确定而非盲重试。
3. 文件提交后，释放同步文件锁，才等待异步运行时应用；不持 threading 锁跨网络 await。操作许可在此过程维持，维护切换不能同时开始。
4. 热应用部分失败保留已保存配置与组件回执；不暗中回滚已应用组件。只对仍匹配磁盘版本的失败项重试，若版本变化返回冲突。纯需重启的字段不假称热应用成功。
5. HTTP 等待取消不撤销已提交的事实。提交线程开始后必须被任务拥有者观测到结束；操作许可在真实线程完成后才释放，不因 `to_thread` 的 await 被取消就认为写入已停。超时返回 pending，随后通过 request ID 查询。
6. 进程重启先读取磁盘并正常装配，应用回执按新的启动身份重新建立；不能用上次进程的 applied 状态声称本次已应用。原子替换只保证单文件替换，既有多文件 best-effort 回滚不能写成崩溃原子事务；崩溃恢复需要核对真实文件集。

同一 request ID 与相同变更返回原回执/进行中状态，不重复保存；不同变更复用 ID 返回冲突。落地时持久化脱敏的请求指纹与回执索引，禁止把明文秘密写入任务记录。无法解析或恢复旧回执时返回未知，不承诺跨重启恰好一次。

## 首批迁移与验证

已迁入通用文本补丁、可信文档校验与全字段保存事务。`FileConfiguration` 持有文件路径、运行配置读取端口和可选的纯分组合并规则；复用共享文件锁，将读文件、针对当前值合并、校验、磁盘替换与所保存字段同步放在同一事务中。音量与 WebUI 保存都走这一实现，其他环境覆盖不被覆盖。构造/导入不加载配置，自动串门合并规则由输入边界注入，不产生维护对行为实现的依赖。

`ConfigurationChange` 在输入边界解析为分组、来源和字段路径，冻结嵌套输入，避免校验完成后又被原始字典修改。它只确认保存结构，不授予编辑权限；字段白名单和语义规则由相应入口批准。`ConfigurationSaveReceipt` 只列出保存字段，业务组件的热应用由应用装配继续处理。音量的十进制字符串兼容与范围仍由 `OutputVolume` 唯一维护。

公开入口证据见 `tests/operations/test_configuration.py`：保留正文/环境覆盖、跨组与嵌套字段保存、输入快照、磁盘失败与动态目标拒绝。原编辑器 AST 测试已直接调用公开文档能力；自动串门并发保存通过注入的运行配置端口验证串行，不再 patch WebUI 私有同步函数。

字段展示和准入继续由 HTTP 输入边界处理，现有响应保持兼容。涉及凭据写入时保留原文件权限与符号链接行为，不把模型环境密钥搬进 WebUI。

现有证据测试：`tests/test_config_editor.py`、`tests/test_config_file_store.py`、`tests/test_voice_settings_reload.py`、`tests/test_auto_visit_config_editor.py`。新增公开行为测试需验证：非法输入不写；并发版本冲突；保存成功/热应用失败可查询；重试不应用旧版本；提交中请求取消不释放许可；进程重启不沿用旧 applied 状态。用 tmp_path 和模拟运行端口，不写当前配置。此处只是待实现验收，现有测试不证明目标类型/API 已存在。

配置加载验证见 `tests/operations/test_voice_configuration_loading.py`：显式隔离来源下的环境优先级、默认/限值兼容、密钥脱敏及独立子进程中的导入无凭据加载。原设置重载、语音字段配置、共享认证 token 与 Aliyun `.env.local` 无变量插值测试继续回归。
