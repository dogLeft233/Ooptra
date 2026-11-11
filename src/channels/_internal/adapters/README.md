# 平台与文字协议适配

公开入口见 [channels.api](../../api.py) 与 [包工厂](../../__init__.py)。本能力隐藏 Oopz SDK、OneBot 编解码和身份映射、代理传输、名称缓存及登录。内部按确有不同知识的 `bridge/`、`oopz/`、`onebot_v11/` 组织；旧三个顶层目录已删除，没有重导出兼容模块。

## 接口和知识归属

- `create_platform_gateway` 提供 `PlatformGateway.start/stop/restart/snapshot`。监督循环串行重建 SDK/OneBot 连接；外部重启只通知工作者，启动中到达的通知不会被清掉。快照返回独立数据，正常运行与外部连接状态分别表达。
- `VoiceLifecycle` 是装配传入的窄端口，渠道只通知 bind_bot、unbind_bot、set_bot_ready；不导入应用或取得全局 runtime。SDK 对象只通过这个明确装配能力交给同属渠道的语音绑定，不供 HTTP 查询读取。
- `PlatformConfiguration` 由维护实现，以配置快照、现读私钥、登录账号及凭据持久化提供能力；SDK 配置构建只作协议映射，不保存文件或读取环境。凭据序列化成已知字段后交维护；原 OneBot 身份映射库及迁移逻辑保持本模块拥有。
- `PlatformDirectory` 为管理端提供脱离 SDK 对象的域/频道/成员投影及实时广播快照，为行为提供不可变成员事实。`name_directory` 只开放缓存读取与显式补全，没有写缓存和 SDK 网关绑定权限。未知实时状态退为未知，不能伪造未静音。
- `create_platform_login` 提供 `PlatformLogin`：API 登录、浏览器任务状态/取消、脱敏凭据摘要。浏览器任务由登录服务持有，HTTP 只校验请求及展示结果；SDK 登录后必须先确认维护持久化，再请求重连。关闭等待浏览器资源释放。

## 内部地图

`bridge/app.py` 负责 SDK 账号和 OneBot 监督装配，`state.py` 保存连接/流量观察，`onebot_server.py` 增加 SDK server 的状态回报，`shim.py` 实现补充协议所需的窄网关。`oopz/sdk_config.py` 隐藏 SDK 配置，`proxy_transport.py` 隐藏外部 HTTP/WS 代理差异，`name_resolver.py` 管理名称缓存，`login.py` 管理外部鉴权。`onebot_v11/` 拥有项目补充事件和身份映射兼容迁移。`voice.py` 的文字发送适配先用目录确认显式目标，再调用 SDK 实际接口 `messages.send_message(text, area=..., channel=...)`，转换平台 message_id，不把 SDK 响应交应用。

账号绑定事实、房间事实和模型任务分开：本能力只管理账号/事件连接；房间会话与播放仍归上层 channels 会话/输出能力。文字接入 Oopz→OneBot→AstrBot→OneBot→Oopz 原路径保持，AstrBot 是该路径的对话拥有者，本地不再次生成同一条消息。

共享画面的观看连接由浏览器适配拥有：复用同时匹配会话键、应用、房间、令牌、观看者及发布者；清理前固定操作代次，退房使等待中的旧观看请求失效。抽帧只使用 `requestVideoFrameCallback` 确认过的解码帧，三秒内没有新帧即不可用；反复捕获不会刷新旧图年龄。内部回执携带 `frame_age_ms`，渠道校验后保守恢复原捕获时间，不把 RPC 返回时间当作新画面。验证入口为 `tests/browser/screen-viewer-lifecycle.cjs` 和 `tests/channels/test_screen_capture.py`。

## 验证

`tests/test_bridge_restart.py` 经公开工厂、真实 SDK 装配和临时身份库测试重启/失败/启动通知，仅替换外部 SDK 连接循环；`tests/test_astrbot_bridge_routes.py` 验证鉴权与文字频道映射复用；`tests/test_voice_api_routes.py` 和 `tests/test_voice_member_live_state.py` 验证成员及实时广播投影。真实平台连接和双 bot 声音交互另由验收脚本完成，模拟不能代替真实服务验证。
