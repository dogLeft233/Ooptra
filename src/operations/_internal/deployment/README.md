# 部署任务与受管执行

状态：现有维护门面、更新网络适配和受管父进程已迁入 `service.py`、`network.py`、`supervisor.py`，应用与父进程是此能力的不同角色。公开入口见 [系统维护](../../README.md) 的 `api.Maintenance`、`api.UpdateTransport`、`api.Supervisor`。下文版本化计划、CAS、显式 quiesce/handoff 为后续目标设计，不能视作当前实现。

## 边界与结构

规划、任务台账、父子交接和失败恢复共同拥有一次维护的完成语义，放在同一子模块。若将阶段状态分给 HTTP、worker 和 launcher 各自维护，无法判断谁可继续执行。因此仅有一个持久化任务事实，HTTP 只是查询者。

内部按角色分文件即可：`planning.py` 解析预检/目标，`jobs.py` 管理持久化状态转移，`supervisor.py` 管受管进程，`adapters.py` 封装 OS/Git/安装/健康探测。不是四层公共接口，也不让每个适配自己更改任务阶段。

## 内部接口及数据

```text
Deployment.plan(request: ParsedMaintenanceRequest) -> MaintenancePlan
Deployment.accept(plan_ref) -> JobRef | PlanExpired | Conflict
JobStore.claim(plan_fingerprint, expected_state) -> JobClaim
JobStore.transition(claim, expected_revision, event: JobEvent) -> JobSnapshot
Supervisor.handoff(request: HandoffRequest) -> HandoffAccepted | HandoffRejected
RuntimeControl.request_exit(handoff_ref: HandoffRef) -> ExitAccepted | ExitRejected
Supervisor.run(claim: JobClaim) -> JobSnapshot
```

所有 I/O 异步或在线程/子进程中；`JobStore` 是唯一任务记录写入口。`Supervisor` 是本模块内部对象，不暴露可杀任意 PID 的接口。父进程从经过验证的持久化 handoff 消费任务，不把 HTTP 传来的 PID 当进程所有权。

| 类型 | 字段与约束 |
| --- | --- |
| `MaintenancePlan` | ID、具名动作、原版本/配置指纹、目标 SHA 或 BackupRef、检查项、有效期、影响；不包含任意 shell 文本 |
| `JobClaim` | job ID、plan fingerprint、记录 revision、owner role/进程启动标记；同一计划重复受理关联原任务，不同活跃任务冲突 |
| `HandoffRequest` | job ID/revision、原子阶段、旧进程身份、新/旧环境引用、已校验备份、健康目标；敏感 token 仅私有文件，公开 JobSnapshot 脱敏 |
| `ProcessIdentity` | PID 加本次启动标记/解释器路径；PID 可复用，不单独作为所有权证据 |
| `JobEvent` | 有限阶段转移及脱敏原因，带 expected revision；父/子只能写自己阶段，不以最后写入覆盖竞争 |
| `JobSnapshot` | 阶段、进度、目标、恢复结果、错误及可操作提示；unknown/corrupt 不能等同 complete |

`JobStore` 需在既有 job 文件兼容基础上新增 schema/revision/owner 校验；不承诺现有纯 JSON 读改写已经跨进程 CAS。落地可以采用单写者交接：应用独写 preparing/awaiting-restart，父确认子已退出后独写 switching 以后阶段。读取后原子替换不是跨进程锁；如需并发写，先实现并测试部署锁，不能靠时间戳解决。

operations 外观的进程内 mutation gate 还必须核对持久化 active-job 与当前进程角色。候选进程 bootstrap 时先读取此状态：父仍在 switching/checking/rolling-back，或记录未知/损坏时，禁止配置提交、新维护、恢复与备份清理；只开放查询/health 和恢复诊断。父写 complete/rolled-back 并交还控制后才能解除限制。不能因候选拥有一把新的内存锁就允许写入。业务入口也经 app 激活许可保持只读/暂停自动行为，bootstrap-ready 与“可接受新业务副作用”分开。

## 状态机与资源顺序

```text
planned → preparing → awaiting-restart → quiescing → quiesced → awaiting-exit
awaiting-exit → switching → checking → complete
preparing → failed（尚未quiesce，旧服务继续）
quiescing → quiesced → awaiting-exit
quiescing → blocked-degraded（部分关闭，禁止切换）
switching/checking → rolling-back → rolled-back | failed（需人工恢复）
崩溃/记录损坏/身份不符 → interrupted/unknown（先核对，不自动重做）
```

阶段名以目标类型枚举为准；兼容适配映射既有 `awaiting_restart/rolling_back` 等字段，不能直接改旧 worker 读取的字符串后遗留另一侧。

1. 受理前取得 operations 内部独占变更许可，复查计划指纹/有效期/预检；持久化 claim 成功才启动任务。准备线程的生命周期归 Deployment，HTTP 超时不取消或重复创建它。
2. 在线准备新环境和验证备份；期间只读取当前部署，不替换代码或恢复数据。准备结束再次复查原版本/配置；若变化则失效计划，不能沿用过时恢复目标。
3. 写入 awaiting-restart 后进入 quiescing，通过注入的 `RuntimeControl.quiesce` 关闭新业务请求、调度与业务资源，保留本地管理/交接通路。需要从仍运行的数据库提取任务/outbox 状态时，在关闭前完成；模块自行负责 drain/持久化。部分关闭后失败进入 blocked-degraded，保留已关闭/未关闭清单，不再声称旧服务正常继续；不切换、不盲目恢复单个资源，先通过显式重启原部署或经验证的恢复流程恢复，保留任务证据。
4. quiesce 成功后持久化 quiesced 与不可变 HandoffRef，确认父交接条件，然后写 awaiting-exit 并调用 `RuntimeControl.request_exit(handoff_ref)` 通知主循环 graceful exit。主循环负责结束 WebUI 和应用进程；不能关闭管理通路后再依赖它请求退出。退出承诺后 HTTP/等待者取消不撤销 handoff。父确认自己拥有的旧子进程真正退出，接管单写者角色，建立最终安全备份，然后切换版本/恢复数据、启动候选进程。父发现子未退出或身份不符时拒绝切换并报告 blocked，不强杀不归自己的进程。子进程交接后不能继续写 job。
5. 健康检查核对候选身份、解释器、启动标记、bootstrap-ready 与本地连接监督器存活；外部 Oopz/OneBot 离线只表示降级，不触发版本回滚。
6. 成功后才记录新环境为 canonical；失败先停止自己拥有的候选进程，再恢复原版本、数据和环境，重新检查原进程。回滚仍失败则保留快照与具体人工恢复入口，不循环无限重试。

资源关闭在新请求受限后按业务拥有者执行；不在持任务文件锁期间等待 HTTP/进程。准备线程不能被 asyncio 取消就视为停止，父进程必须等待完成或拒绝交接。原子文件只保护记录完整性，崩溃恢复必须核实阶段及进程/磁盘真实状态，不以“没有异常”推断维护已完成。

## 依赖与首批切片

本子模块可调用 [备份能力](../backups/README.md)，读取 [配置检查](../configuration/README.md) 的只读指纹，并使用 operations 公共管理端口；后两者不反向调用部署。operations 外观持有小型 mutation gate 协调“配置提交/热应用”和“维护受理”，只负责互斥，不再存一套任务状态机。

查询、台账、准备和父进程监督已迁入；WebUI 只保留 HTTP 适配，旧 `maintenance_worker/network/storage/cleanup` 文件已移除。当前仍读写原有 JSON schema 和阶段名，准备完成后发出停机回调、父确认退出后切换；尚无目标版本化计划和显式 handoff 对象。手动 main.py 仍明确不支持受管切换。

现有证据测试：`tests/test_maintenance_worker.py`、`tests/test_maintenance_atomic_status.py`、`tests/test_maintenance_deadlines.py`、`tests/test_maintenance_health.py`、`tests/test_maintenance_rehearsal.py`。需新增目标端口行为：重复计划只受理一次、配置变化使计划失效、准备中 HTTP 超时、交接单写者、PID 复用拒绝、候选降级成功、checking期间新进程写请求被拒、quiesce后确实触发退出、部分关闭失败不伪报旧服务正常、回滚失败保留证据。隔离 fixture 模拟进程和文件；不在当前部署执行真实升级。

`MaintenancePlatform` 注入外部时钟、命令、网络、存储与健康查询。备份/删除/清理的线程由服务观察至真实结束，等待者取消不释放正在使用的变更许可。准备线程由 `close` 等待，不允许父进程切换与仍在准备的线程并行。现有隔离 `test_maintenance_rehearsal.py` 继续验证真实子进程切换与候选失败回滚，不在当前仓库执行升级。
