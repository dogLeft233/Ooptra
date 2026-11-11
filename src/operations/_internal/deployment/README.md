# 部署任务与受管执行

`service.py` 拥有准备及查询流程，`jobs.py` 是任务 revision/owner 的唯一正常写入口，`supervisor.py` 拥有自己创建的进程和代码切换，`network.py` 只处理可信更新源与下载。公开入口见 [系统维护](../../README.md)。

## 计划、互斥和台账

`MaintenanceRequest` 只允许已解析的更新目标或备份 ID。计划持久绑定配置摘要、原提交、目标、有效期和影响；执行时再次复核。同计划保存执行任务绑定，重复请求返回同 `JobRef`，历史任务仍可查询。计划不授权任意 shell 文本。

配置提交/热应用与维护受理共用 `mutation.lock` 的跨进程 OS 文件锁。应用拥有准备线程，即使 HTTP 取消，也等真实 I/O 完成才释放；不持任务记录 CAS 锁等待业务静默。任务记录使用 schema=2、revision 和 owner，旧 revision 或错误角色不能覆盖新阶段。应用写准备至 awaiting_exit，父确认原进程退出后独写切换阶段。查询与历史记录不产生第二份状态机。

## 状态和交接

```text
preparing → quiescing → quiesced → awaiting_exit
awaiting_exit → switching → checking → complete
preparing → failed
quiescing/quiesced/awaiting_exit → blocked_degraded
switching/checking → rolling_back → rolled_back | failed
启动器中断活跃任务 → interrupted（保持受限）
```

准备只验证新环境、归档与依赖，不替换运行代码。准备后复核配置，调用应用的 `RuntimeControl.quiesce(job_id, deadline)`。部分失败保留资源清单，禁止切换和新业务；不得宣称旧服务仍正常。完整静默后耐久写交接 token/revision，再请求退出；退出请求失败也保持 blocked_degraded。

父进程 `confirm_stopped` 必须验证自己拥有的进程已经退出、PID 和本次随机启动标记匹配，并消费 awaiting_exit 的交接 token。`switch` 需要具名 `StoppedDeployment`，不再接受只有 PID 或旧 awaiting_restart 的隐式停机。最终安全快照在旧数据库关闭后建立。

候选主循环从持久 gate 保持业务未激活，HTTP 写请求也受 gate 限制。健康检查核对 PID、启动标记、解释器及本地装配；matching 候选可声明正在等待父提交，不要求提前连接外部平台。父写 complete/rolled_back 后才激活。普通启动仍要求桥接监督器；外部连接暂离线不触发回滚。

失败先停止自己拥有的候选，再恢复原版本/数据并核对原环境。回滚失败保留安全备份及人工恢复说明。中断、损坏和未知记录保持受限；启动器不能通过改成普通 failed 自动开放业务。

## 验证

`tests/operations/test_versioned_maintenance.py` 覆盖版本、幂等、变更屏障、取消、部分静默和进程身份。`tests/test_maintenance_rehearsal.py` 在隔离 Git 仓库启动真实子进程与 HTTP 服务，覆盖等待父提交、候选崩溃、未就绪、无监督和回滚。真实部署、账号、更新源和数据不用于故障注入。

维护计划的变更锁、配置摘要和计划落盘在线程内执行，复用维护模块拥有的 `_run_mutation` 取消收尾。锁忙明确拒绝，不在事件循环阻塞等待正在异步应用的配置事务；重复取消也等待已受理落盘完成。
