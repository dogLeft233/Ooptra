# 来源准备与 AstrBot RPC

[bridge.py](bridge.py) 保留 protocol=1 的准备、工具、绑定管理和 HTTP 传输；[service.py](../service.py) 捕获不可变来源并签发授权。接口与类型见 [上下文公开入口](../../api.py)。来源规则和长记忆归 AstrBot，不复制可独立修改的 group_map。

## 准备与工具

`bridge.prepare(area,channel)` 先提升来源会话代次，结果只安装到原会话；切换后的迟到结果丢弃。`prepare_context` 捕获来源完整快照，返回 `ContextRef`、人设、版本与历史。`tools(prepared)` 仅提供原快照及本次授权工具能力，不把可变 bridge 交生成。

`RecallRequest` 绑定来源和工具 ID。`RememberRequest` 还须提供 context 已登记的 `WriteAuthorization`；授权来自完整且稳定说话者的明确原输入，不能由模型造同形对象获得权限。授权 120 秒过期，刷新来源立即撤销新调用。模型只给 query/content，不选择任何 UMO、人格或平台目标。既有管理员对私聊共享/写回的授权仍由插件来源规则负责。

显式写入先经 SQLite 台账提交，再发布领取；queued 只表示本地耐久受理，外部确认之前不能称已长期保存。工具 ID 对应原请求 ID，同 ID 异内容冲突；短等待读取与耐久投递分离，取消等待不删除已受理写入。

## HTTP 与租约

HTTP 只负责认证和解析：`PollRequest` 区分 events/timeout/peer_conflict/closed；`RpcReceipt.parse` 保存不可变 JSON 快照，`resolve_receipt` 内部验证 peer 并区分 accepted/duplicate/unknown/rejected；`BindingRequest.parse` 限制既有 list/save/delete/source-save/source-delete/source-options。`events()` 每次返回独立字典。

保留 pending 最多 16、每批最多 8、poll 最长 20 秒、普通读取租约 8 秒、写/配置租约 25 秒、peer 在线 35 秒。bindings 不受语音切房取消；close 才结束全部短等待。outbox 原始事件/回执由台账持久化，内存仅是可重建工作集，租约无需跨进程保留。

投递部分确认时台账按 UMO 单调合并；重启后 request ID、原房间与原来源不变，只传未确认目标。clear 不清耐久投递，也不把旧目标重定向到新来源。RPC/来源的旧字典接口保留为 protocol=1 兼容适配，生产模型通过绑定工具端口，不能用旧 tool 绕授权。

## 验证

`tests/context/test_transactional_context.py`、`tests/context/test_sources_contract.py`、`tests/test_astrbot_context_bridge.py` 验证准备迟到、明确降级、快照隔离、原 ID 重试、部分确认重启、坏文件、授权、peer 和 bindings 跨切房。外部真实插件长期记忆存储不能由这些隔离测试代替。
