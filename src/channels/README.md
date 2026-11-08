# 渠道交互

本模块拥有平台协议、实际房间和音频执行状态。先读 [公开接口](api.py)、[类型](models.py) 和 [工厂](__init__.py)，再按下表选择内部能力。全局规则见 [系统架构](../../ARCHITECTURE.md)。

## 内部能力地图

| 能力 | 隐藏的知识 | 入口与详细说明 |
| --- | --- | --- |
| 平台与文字代理 | Oopz/OneBot 编解码、连接重试、登录、身份映射与目录查询 | `create_platform_gateway` / `create_platform_login`；[adapters](_internal/adapters/README.md) |
| 账号和房间 | SDK/浏览器绑定、共享传输收尾、会话标识、串行转换、真实状态 | `create_voice_binding` / `Channel.execute`；[session](_internal/session/README.md) |
| 输入与输出 | 跨线程采集、白名单、VAD、格式/序号、背压、播放与音量 | `create_audio_ingress` / `Channel.play` / `OutputControls`；[output](_internal/output/README.md) |

渠道不负责语义识别、接话选择、记忆或配置保存。SDK 对象只作为装配输入留在适配内部；公开目录不返回 bot、浏览器或 ID 数据库句柄。系统维护通过注入端口保存配置，渠道分别确认实际音频应用结果。

## 对外契约

- `PlatformGateway` 提供启动、停止、重启和快照。连接变化通过注入的 `VoiceLifecycle` 通知装配；业务不反向导入应用单例。Oopz → OneBot → AstrBot 的文字代理保留原身份映射和协议，外部回复不会再次触发本地生成。
- `PlatformDirectory` 将 SDK 字典/对象解析成只读目录和具名成员事实。confirmed 的空结果、unavailable 和 invalid_response 分开；不把畸形响应容错成“空房”。`is_self` / `is_bot` 在平台边界计算，未知成员不推断为真人。
- `VoiceBinding` 拥有账号、浏览器、捕获回调和退役任务。重复绑定同一对象不重建；换绑撤回旧输出，复用共享传输前等待旧会话收尾。失败清理保留降级事实，不能丢引用后假称已恢复。
- `Channel.execute` 返回房间/输出控制等动作的真实结果，`snapshot/outcome` 查询当前事实和原动作。`RoomTarget` 是目标，`SessionRef` 是不可变身份；二者都不允许调用方修改房间真相。
- `SendText` 是独立文字动作，携原 SessionRef、显式目标及可选 parent_action_id；执行前向平台目录确认目标为 TEXT/CHAT，不使用语音房或猜测首个频道。成功返回 media=text 和平台 message_id；发送效果不确定时保留原动作结果，不自动重试。文字与音频完成分别记录，不能互相替代。
- `Channel.play(request, frames)` 接管单消费源及关闭，`playback/cancel_playback` 查询/撤销原动作。帧序号、格式、会话和截止逐次复查，正常源终态后 finish，再等实际排空；源失败不能当 EOS。
- `AudioIngress` 在模型输入前完成监听授权、采样校验、成员窗口及切句。输入队列缺口使旧采样窗口失效；概率、关键词、回应对象偏好归行为模块。

音频、平台生命周期、命令返回和 Live 控制流具有不同的消费/背压语义，保留各自窄端口。没有增加应用不需要的通用事件总线，也不让多个模块竞争同一音频 queue。

## 容量、取消与证据

房间动作台账最多 128 个活动项、1024 个总项，已接受终态至少保留十分钟；满载拒绝新动作，重复等待和查询仍可用。播放是会话内单播放器，结果分别记录提交帧/采样、确认采样及未知范围；最近拒绝也保留查询。具体预算和取消语义由公开接口定义，不承诺跨进程幂等或人耳实际听到。

同 ID/同命令重入等待原执行，异载荷冲突；重复等待者取消只撤销等待。已开始的副作用不能因调用者取消而假称未执行。未开始就取消也关闭源并保留终态；停止/关闭不合作时隔离输出，不复用可能继续出声的传输。

应用中的 `VoiceCoordinator` 串行的是跨模块请求和流程撤销；房间锁、真实会话、播放任务和音频资源在本模块。界面上的 joined/speaking 是快照或流程投影，不是另一份可修改的渠道状态。

## 验证

`python -m pytest tests/channels -q` 验证公开房间、目录、输入、播放与清理契约；[重绑定回归](../../tests/test_voice_duplex_rebind.py) 使用真实音频 pump 和外部传输替身；[平台重启](../../tests/test_bridge_restart.py) 使用真实 SDK 装配与临时身份库。其他集成见 [应用导航](../app/README.md)。

新增反例覆盖未开始取消、重复源、过期查询、帧缺口/越界、迟到输入、满队列断点、未知推送范围、不合作源、停止未确认和 SetOutput 取消。静态检查只辅助依赖约束，真实语音效果仍按 [双机器人验收](../../docs/bot-pair-acceptance.md) 核对。

现有 `reply_text_to_channel` 原先只有配置/UI 字段，本轮才实际接通。装配只使用已显式配置的 `OOPZ_CONFIG.default_area/default_channel`；未配置时应用报告 `target_unavailable`，不伪造渠道动作。字幕发送仍由渠道执行前查证频道类型；若配置指向语音频道就拒绝。验证见 `tests/channels/test_text_action_contract.py` 和 `tests/app/test_subtitle_outputs.py`，覆盖独立成败、取消/未知效果、用户输入和助手历史只记录一次。

公开值的反例验证见 [输出值边界](../../tests/channels/test_public_output_guards.py)：动作身份必须是非空字符串，完成证据不能含未确认样本；采样率和帧窗口必须符合输入/整句预算。非法值在构造时拒绝，后续执行直接消费已解析结构。

## 共享画面

`Channel.capture_screen(session)` 在五秒内尝试取单一屏幕分享者的当前 JPEG，返回 `ScreenCapture`。无共享、多人共享、失败和旧会话分别报告；并发捕获不排队。Oopz 适配查询当前频道分享者，通过独立 Agora audience 观看连接取得最新帧，不发布音视频；退房关闭观看连接。图片至多 1 MiB、最长边 1920，只在当前回复中使用，不缓存编码图片或写入历史。协议和切房反例见 `tests/channels/test_screen_capture.py`，真实视频解码见 `tests/browser/screen-frame.cjs`。

`PlaybackRequest` 分别定义首段音频等待（默认45秒）和后续断流等待（默认10秒）。源帧等待使用对应预算，传输确认最多10秒、排空确认最多30秒，均受请求绝对截止约束。超时结果区分 `first_audio_timeout`、`audio_gap_timeout`、`transport_timeout`、`drain_timeout` 与整轮 `deadline`；失败停止并关闭源，部分提交保留unknown_samples。公开验证见 `tests/channels/test_playback_timeouts.py`，含真实等待超过旧10秒的反例。
