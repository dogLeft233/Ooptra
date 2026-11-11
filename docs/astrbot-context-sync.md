# AstrBot 上下文同步

适用：Ooptra main 4.0.0、beta 261019-beta、dev 261019-dev 与 astrbot_plugin_ooptra >= 2.0.0，协议 v1。

## 运行结构

插件用现有 Ooptra 地址、Bearer Token 建立 HTTP 长轮询，不额外监听端口。

| 接口 | 用途 |
| --- | --- |
| GET /voice/astrbot/events?client_id=… | 插件领取 prepare、recall、remember、commit 请求，最多等待 20 秒 |
| POST /voice/astrbot/result | 回传 request_id、client_id 和实际执行结果 |
| POST /voice/astrbot/refresh | 清理当前模型会话并重新解析来源 |
| GET /voice/astrbot/bindings | 通过插件读取唯一的 group_map 配置 |
| POST /voice/astrbot/bindings | action=save/delete 管理完整 UMO 映射；source-save/source-delete 管理来源，source-options 查询目录 |
| GET /voice/astrbot/text-channels?area=… | 返回命名文字频道、现有 OneBot 数字 ID 和 bot_id，用于核对 WS 平台身份 |

WebUI 同时提供 /api 前缀别名，独立 VOICE_API 使用相同路由与自己的令牌。
持久 client_id 为当前插件实例提供租约；其他实例在租约有效时被拒绝。
插件在关闭人格同步时仍轮询，以提供绑定管理；prepare 和记忆操作仍受同步开关限制。
bindings 管理请求独立于语音 session_id，自动串门不取消正在保存的配置。

请求包含 protocol、request_id、session_id、room、scope、created_at 和 payload。
room 是实际 Oopz 域／频道；scope 保存完整人格 UMO、人格、主记忆插件、memory_scopes 与 memory_route_id（来源规则摘要）。memory_scopes 含所有选中会话的完整 UMO、平台身份、群聊／私聊类型和各自 memory_persona_id。
作用域变化后丢弃过期的上下文响应。普通记忆采集保留原始来源，不会重新定向到新群。

## 人格与记忆边界

人格来源单选，可手选内置人格覆盖；记忆会话多选，查询与写回全部选中目标。每个目标独立跟随其会话人格过滤。控制映射与来源规则分离，频道规则覆盖域默认，旧唯一绑定仍兼容；显式空规则不回退。私聊须管理员确认共享和写回。

Live 使用系统指令、初始群记忆和显式记忆工具。记忆请求在独立任务中执行，
不会在模型收包循环中等待第三方检索。模型取消尚在排队的工具调用时，取消相应任务。
已经由第三方开始执行的写入不能据此撤销；工具结果未确认时不会承诺已保存。

MiMo 使用相同来源人格和群作用域，检索后加入受限上下文，并处理记忆工具结果。
公告、API 快捷开口、未播放回复和被打断的助手全文不按完整普通对话采集。
流式级联被打断时，已确认的用户转写仍按原说话者保留；回合开始即由上下文桥绑定原来源与写授权，迟到提交继续投递原目标，不进入新会话历史。

## 本地状态

- voice_memory.jsonl：原有语音记录。
- 同目录 astrbot_voice_outbox.json：待确认的采集事件与失败后补写的 remember 请求，保留原请求 ID，最多 128 条。
- 配套插件数据目录 voice_context.sqlite3：来源线索、实例 ID、处理回执与阶段检查点。

离线时，已建立的 Live 会话仍保留已加载人格；记忆请求明确失败，语音采集继续排队。
重进房或刷新上下文会重新选择来源。入队、落盘和读取失败会显示诊断，不伪报长期记忆成功。

回执保护正常重试和重连；外部插件写入成功到本地检查点落盘之间仍有崩溃窗口，
不能承诺跨进程、跨第三方存储事务的严格 exactly-once。
逐目标保存成功回执，部分失败重试只补写失败目标。全部成功才返回整体 ok=true；普通 captured 表示对话已进入采集，stored 才表示明确长期写入已确认。

## 验证

```text
python -m pytest -q
ruff check .
python scripts/build_webui_preview.py --check
node tests/browser/webui.cjs
node tests/browser/preview.cjs
```

新协议和 Live 测试位于 tests/test_astrbot_*.py。跨仓库联调、桌面与窄屏页面检查
使用无真实凭据的本地测试服务；这些检查不等于真实 QQ 群和 Oopz 房间验收。
