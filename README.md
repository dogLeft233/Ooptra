<p align="center">
  <img src="./src/webui/assets/logo.svg" width="80" alt="Ooptra 双对话桥 Logo" />
</p>

<p align="center">
  <img src="https://count.getloli.com/@Ooptra?name=Ooptra&amp;theme=minecraft&amp;padding=6&amp;offset=0&amp;align=top&amp;scale=1&amp;pixelated=1&amp;darkmode=auto" alt="Ooptra 访问计数" />
</p>

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="Ooptra：语音对话、自检试听、自动串门与更新备份" />
</p>

<p align="center">
  <a href="CHANGELOG.md"><img alt="更新日志" src="https://img.shields.io/badge/changelog-更新日志-315DDC?style=flat-square"></a>
  <a href="https://github.com/Eason4869/Ooptra/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/Eason4869/Ooptra?style=flat-square"></a>
  <a href="https://github.com/Eason4869/Ooptra/actions/workflows/ci.yml?query=branch%3Amain"><img alt="正式版 CI" src="https://img.shields.io/github/actions/workflow/status/Eason4869/Ooptra/ci.yml?style=flat-square&amp;label=main%20CI&amp;branch=main"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-3776ab?style=flat-square&amp;logo=python&amp;logoColor=white">
  <a href="LICENSE"><img alt="许可" src="https://img.shields.io/badge/license-MIT-3ddc97?style=flat-square"></a>
</p>

# Ooptra

把 Oopz 文字接入 AstrBot 等 OneBot v11 框架，并让机器人进入 Oopz 语音房自然聊天。

当前渠道：**main**，运行版本：**4.0.0**，正式版比较基线：**4.0.0**。正式版通过 `main` 的 tag 和 Release 发布；`beta` 与 `dev` 保留各自的测试渠道。

> 此版本的人格与双向记忆同步功能需配合 **[astrbot_plugin_ooptra 2.0.0 及以上版本](https://github.com/Eason4869/astrbot_plugin_ooptra/tree/dev)** 使用。单独运行 Ooptra 时，仍可使用本地人格和语音记录。

## 可以做什么

| 功能 | 使用方式 |
| --- | --- |
| 文字桥接 | Oopz 群聊、私聊通过 OneBot v11 反向 WebSocket 连接 AstrBot |
| 实时语音 | Gemini Live；MiMo 级联；阿里 ASR/TTS + DeepSeek 分句流式回复 |
| AstrBot 人格 | 单选映射会话跟随人格，也可手选 AstrBot 内置人格 |
| 双向记忆 | 多选映射会话，查询与写回全部选中会话，逐目标确认和重试 |
| 房间管理 | 手动进退房、自动串门、成员状态、抢话、关键词接话、音量与临时闭麦 |
| Web 控制台 | 登录、配置、语音试听、状态与日志、更新、备份和恢复 |

文字回复仍由 AstrBot 处理。Live 的音频对话在 Ooptra 中运行，记忆查询独立于音频收发。

## 安装与启动

需要 Git、Python 3.10+。体验本页开发版：

```powershell
git clone -b dev https://github.com/Eason4869/Ooptra.git
cd Ooptra
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.example.py config.py
```

Linux / macOS 使用 `source .venv/bin/activate` 和 `cp config.example.py config.py`。正式版选择 `main`，测试版选择 `beta`；本页新增的 AstrBot 上下文同步需使用上面注明的配套版本。

在 `config.py` 填写 Oopz 账号，或启动后在控制台登录。需要语音或浏览器登录时，再安装：

```text
python -m pip install -r requirements-optional.txt
python -m playwright install chromium
python launcher.py
```

打开 `http://127.0.0.1:3090`，首次在部署机器本机设置控制台密码。该密码也是配套插件使用的 API Token。

升级时请保留已有 `config.py`、`private_key.py` 和 `data/`，不要重新复制示例覆盖配置。特别是 `data/onebot_v11.sqlite3`，其中的身份映射需要延续使用。

## 接入 AstrBot

1. 在 AstrBot 新建 OneBot v11 反向 WebSocket 平台。
2. 在 Ooptra「配置 → 连接」填写该平台的反向 WS 地址和相同的访问令牌。
3. 点击保存并重新连接，确认概览中显示已接入。

反向 WS 地址默认示例为 `ws://127.0.0.1:6200/ws`，以 AstrBot 实际配置为准。跨机器部署时，填写部署机器之间能访问的地址。

## 开始语音对话

1. 在「配置 → 语音模型」开启语音，选择 Gemini Live、MiMo 或阿里语音 + DeepSeek。
2. 填写对应密钥、模型和音色。
3. 在「语音台 → 会话控制」选择域和频道，然后进房。
4. 使用「自检与试听」先检查环境、音色及进退房台词。

默认语音回复概率为 30%。强制关键词、连续追问窗口、监听成员范围和抢话门限均可配置。手动开口、语音退房、音量和临时闭麦规则见[语音指南](docs/voice-guide.md)。OpenAI Realtime 尚未接入。

阿里语音 + DeepSeek 的密钥从进程环境或项目根目录 `.env.local` 读取：
`DASHSCOPE_API_KEY`、`DEEPSEEK_API_KEY`。密钥不进入前端配置；模型与流式行为见[接入说明](docs/voice-guide.md#阿里-asrtts-与-deepseek)。

## 与 QQ 群共享人格和记忆

在 AstrBot 安装配套插件，然后在它的工作台完成：

1. 将插件 `api_base` 指向 Ooptra，例如 `http://127.0.0.1:3090`，`api_token` 填控制台密码。
2. 到「会话对应」选择域／语音频道，映射多个完整 UMO，包括真实 QQ 群、Oopz 文字频道或私聊；逐会话设置是否开放控制。
3. 开启「人格与双向记忆」并选择主记忆插件。在「会话对应 → 人格与记忆」单选人格来源、多选记忆会话；查询和写回均覆盖全部选中会话。
4. 重新进房，或点击「刷新当前语音上下文」。刷新会重建当前模型会话。

例如，在 QQ 群记录一次活动约定后，可以在绑定语音房询问这项约定；语音中的新对话会交给同一个记忆插件，供之后 QQ 聊天使用。普通聊天的长期总结由记忆插件决定时机；明确要求“记住”时，只有收到成功结果才可确认写入。

当前适配 **Memory Companion** 和 **LivingMemory**。自动检测针对已实现适配器的插件及其实际接口；同时启用多个记忆插件时，需要选择一个主插件。权限校验、白名单或群采集设置阻止访问时，工作台会显示原因。

### 来源如何确定

- QQ 指令、控制台进房、自动串门按实际语音房匹配独立来源规则；指定频道优先，否则继承域默认。
- 一个域／频道可映射多个群聊和私聊。完整 UMO 如 `Bot:GroupMessage:12345678`、`Bot:FriendMessage:87654321`，可区分不同平台实例的同号会话。
- 人格来源只能选一个，可手选内置人格覆盖；记忆来源最多选 8 个，每个目标按自身会话人格过滤，查询全部、写回全部。
- 页面按名称选择 QQ 群、私聊联系人和 Oopz 文字频道，并校验 Oopz WS 实例。其他已启用平台可填写完整 UMO，是否支持记忆由相应记忆插件决定。
- Oopz UID 与 QQ 号需显式关联，才能准确标注个人身份；未关联者使用 Oopz 来源身份，不根据昵称猜测。
- 私聊来源需要页面明确确认向公共语音共享上下文及写回私聊。成员独立会话需提供实际完整 UMO。
- 旧数字群号兼容唯一绑定回退；一旦保存独立来源，即使删除全部规则也不会回退。旧轮次不会因修改来源而改写到新会话。

在 Ooptra「语音台 → 会话对应」维护映射和来源，「人格与记忆」查看生效人格、记忆会话和待写条数。每个记忆目标单独确认，部分失败仅补写失败目标；全部成功才可确认全部写入。不选择记忆会话时仅同步人格。本地 JSONL 记录独立管理，清空本地记录不会删除 AstrBot 长期记忆。

## 自动串门与日常维护

在「语音台 → 自动串门」逐域启用。检查间隔、进房概率、停留时间、进退房台词、冷却和每日上限可以调整；默认全部关闭。当前实例同一时间只维护一个语音房会话。

控制台「更新与备份」提供 `main`、`beta`、`dev` 频道检查、备份及恢复。自动部署请通过 `python launcher.py` 启动。局域网访问需设置访问密码，并将监听地址改为 `0.0.0.0`。

| 需要帮助 | 文档 |
| --- | --- |
| 语音、播放、试听和 API | [语音指南](docs/voice-guide.md) |
| 自动串门 | [串门说明](docs/voice-guide.md) |
| 配置、数据文件、启动和升级 | [配置与维护](docs/operations.md) |
| 同步协议、适配边界与验证 | [AstrBot 同步说明](docs/astrbot-context-sync.md) |
| 开发和测试 | [开发说明](docs/repository-audit.md) |
| 版本变化 | [更新日志](CHANGELOG.md) |

## 常见问题

- **进房却不接话**：检查语音开关、密钥、实际连接状态和回复概率；先用强制关键词测试。
- **人格没有变化**：在插件查看选中人格，再刷新上下文；修改本地人格不会覆盖已接入的 AstrBot 人格。
- **没有找到 QQ 记忆**：检查来源群、主记忆插件、人格过滤和插件权限。未检索到不等于该记忆不存在。
- **待写条数增加**：检查 AstrBot 连接和记忆插件状态。语音记录会在本地队列等待确认，重启后继续尝试；队列有上限，错误会显示在状态中。
- **升级后界面仍旧**：先核对版本并强制刷新浏览器。

类型检查、运行边界与持续集成说明见 [Pyright 审计记录](docs/pyright-audit.md)。本版包含 dev 分支 `b851c1a` 的类型与运行边界修正，以及 `a3220a0` 的 Live 可选依赖 CI 修复。

## 一眼看清频道对应

打开「语音台 → QQ 群对应」，按 **Oopz 域 / 语音频道 → QQ 群号** 查看对应关系。域和频道直接选名称，底部预览将要保存的范围；可搜索、编辑或移除已有对应。

默认绑定指定频道。选择「整个域」时会再次确认，因为它涵盖域内全部语音频道；与其他 QQ 群重叠的范围会拒绝保存。已有冲突会在卡片中提示。此页与 AstrBot 工作台共用插件的 `group_map`，关闭人格同步后仍可管理对应关系。

历史多域绑定会完整显示，可在 AstrBot 工作台维护多域范围；不会被此页静默改成单域。更改对应后，下次进房生效；当前房间可在「人格与记忆」点击「刷新 AstrBot 上下文」，会重建当前语音模型会话。

## 许可

[MIT](LICENSE)。Ooptra 是第三方工具，与 Oopz 官方无隶属关系。内置 `src/oopz_sdk/` 的版权归其原作者。请仅用于自己的账号和有权管理的社群。
