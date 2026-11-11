# 配置事务

状态：待实现内部设计；仅供 `operations` 内部使用，不是新的跨模块 API。模块外见 [系统维护](../../README.md)，文件写入机制仍复用 `core.config_file_store`，不复制一份文件锁。现有入口为 `webui.config_editor.apply_updates`、`VoiceRuntime.reload_settings` 及账号/凭据保存路径；本次没有改变这些代码。

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

先提取现有字段解析/文本补丁及保存，用 HTTP 适配保留现有响应；随后替换 `VoiceRuntime._save_output_volume` 的 WebUI 反向导入。涉及凭据写入时保留原文件权限与符号链接行为，不把模型环境密钥搬进 WebUI。

现有证据测试：`tests/test_config_editor.py`、`tests/test_config_file_store.py`、`tests/test_voice_settings_reload.py`、`tests/test_auto_visit_config_editor.py`。新增公开行为测试需验证：非法输入不写；并发版本冲突；保存成功/热应用失败可查询；重试不应用旧版本；提交中请求取消不释放许可；进程重启不沿用旧 applied 状态。用 tmp_path 和模拟运行端口，不写当前配置。此处只是待实现验收，现有测试不证明目标类型/API 已存在。
