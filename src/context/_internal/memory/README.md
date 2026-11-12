# 历史与可靠投递台账

[ledger.py](ledger.py) 通过标准库 SQLite 统一拥有历史、输入/动作幂等、原始 outbox、逐目标回执和导入标记。生产入口是 [ContextService](../../api.py) 的 `observe/record_output/delivery/export_legacy`；来源适配只持有可重建队列与租约，不有第二份耐久 outbox。

## 提交与恢复

单一写锁下开启有限本地事务，历史、幂等载荷/回执、可选投递共同提交；SQLite synchronous=FULL。失败完整回滚，不先确认再写文件。同 ID 异内容报冲突，助手全文只接受真实 completed 执行。输入按 `input_ref` 独立提交，乱序观察先保存输入再检查投影序号；后续输出不重复输入。当前不支持同动作后续追加确认范围；异内容必须显式使用新记录，不能覆盖原确认。

128 个待投递容量满时仍提交本地历史，回执为 `rejected_capacity`；幂等行保留原来源和原载荷。相同记录重试只尝试入队，成功后 DeliveryId 稳定。临时失败为 retrying；永久失败和 confirmed 保留可查询终态。逐目标确认按 UMO 单调合并，重试视图只裁剪已确认目标；规范原始事件永不修改。

## 位置、导入与降级

数据库从原 `memory_path` 派生为同目录 `<原文件stem>_context.sqlite3`，不使用 OneBot 身份映射数据库；更换 memory_path 保持独立历史语义。`PRAGMA user_version=1`，`context_metadata.schema=1`；未知版本或其他数据库拒绝写入。

首次事务校验并导入原 JSONL 与 `astrbot_voice_outbox.json`，以稳定历史导入键、原 request ID 和来源提交，记录条目计数。坏历史/队列使整个导入回滚并拒绝启动；不会当空数据。原文件完整保留但停止写入，导入标记保证重启不重复导入。独立 `create_memory` 兼容视图沿用旧宽容解析，跳过原先不属于历史的坏行；生产 `create_context_service` 严格校验。JSONL 只剩导入/导出格式，没有旧写实现。

`await service.export_legacy(empty_directory)` 在同一 SQLite 快照读取历史与未确认投递，导出 `voice_memory.jsonl`、`astrbot_voice_outbox.json`，最后发布含计数和 SHA-256 的 `context-export.json`。非空目录拒绝覆盖；没有匹配 manifest 的导出不完整。该接口不改原文件或当前数据库。降级时先停止生产写者、导出并核对 manifest，再由明确恢复流程把文件放到旧版本配置路径；直接运行旧版本会忽略新 SQLite 记录。

正常备份必须包含 SQLite 的已提交状态；现有备份使用 SQLite backup API，避免仅复制主文件遗漏 WAL。不能承诺跨 AstrBot 与第三方存储的严格 exactly-once。

## 验证

`tests/context/test_transactional_context.py` 从公开接口覆盖事务失败既无历史也无投递、重启幂等/冲突、满载后补受理、逐目标恢复、保留原文件与降级导出；使用临时真实 SQLite，不用假存储替代事务能力。

`voice_traces` 是独立的持久化逐句诊断表，按完整SessionRef/input_ref更新；与history共用事务机制和备份，但不写模型历史、外部记忆或幂等输出台账。阶段不能倒退，最终转写不能被临时文字覆盖；记录不追溯启用前的输入，无自动条数裁剪，界面通过游标分批读取。旧版本会忽略新增表，`export_legacy` 不导出诊断记录，SQLite完整备份会包含它。

生成历史查询从既有表读取 `user_key/ts`，不改SQLite表结构；房间现场查询带近期时间边界，个人兼容查询保持原筛选。已确认助手输出才成为assistant行；忙碌期间的新识别只增加user行，不自动产生外部记忆投递。
