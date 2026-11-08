"""config.py 的受限读写：白名单字段 + AST 就地改写。

只在 config.py 里改写白名单分组内的字段值，文件其余部分（注释、用户自定义变量）
原样保留；写入前会先做语法与求值校验，失败则完全不落盘。
"""

from __future__ import annotations

import copy
import importlib
import os
from typing import Any

from behavior import merge_auto_visit_patch
from core.logger_config import get_logger
from core.paths import PROJECT_ROOT
from operations import create_configuration
from operations.models import ConfigurationChange, OutputVolume, validate_network_field

logger = get_logger("WebUIConfig")

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.py")

# 分组 -> config.py 中的变量名
GROUP_SOURCES: dict[str, str] = {
    "oopz": "OOPZ_CONFIG",
    "onebot": "ONEBOT_V11_CONFIG",
    "webui": "WEBUI_CONFIG",
    "voice": "VOICE_AGENT_CONFIG",
    "voice_api": "VOICE_API_CONFIG",
    "auto_visit": "VOICE_AUTO_VISIT_CONFIG",
}

# 分组 -> 展示信息：控制台标题与一句话引导（前端渲染用）。
GROUP_META: dict[str, dict[str, str]] = {
    "onebot": {
        "title": "OneBot v11 连接",
        "desc": "本程序作为客户端拨出，连到对端框架的 OneBot v11 反向 WebSocket 服务；地址与令牌要和那边一致。",
    },
    "oopz": {
        "title": "Oopz 账号与目标",
        "desc": "bot 在 Oopz 侧的身份与默认落点。登录凭据会自动维护，跑通后这里基本不用改。",
    },
    "voice": {
        "title": "语音对话 Agent",
        "desc": "Oopz 语音房 Live 对话（语音进语音出）。默认 Gemini Live；可选级联兜底。",
    },
    "voice_api": {
        "title": "语音 HTTP API",
        "desc": "供 AstrBot 插件或本机脚本查询语音状态、进退房、读写人格与记忆。",
    },
    "webui": {
        "title": "Web 控制台",
        "desc": "本页面的监听地址与访问令牌；换端口后要用新地址重新打开。",
    },
}

# 分组 -> 字段 -> 规范。type 决定保存时的类型转换，sensitive 字段不会回传明文。
# tier: basic=常显；adv=收在「高级选项」里；hidden=不给前端（自动维护/只读）。
# adv 字段可用 section 在「高级选项」内再分小节；缺省 section 归入「其他」。
FIELD_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    "oopz": {
        "default_area": {
            "type": "str",
            "label": "默认域 ID",
            "tier": "basic",
            "hint": "OneBot 调用没带目标域时用它兜底",
        },
        "default_channel": {
            "type": "str",
            "label": "默认频道 ID",
            "tier": "basic",
            "hint": "同上，频道兜底",
        },
        "proxy": {
            "type": "str",
            "label": "网络出口",
            "tier": "basic",
            "hint": "留空=跟随系统代理，direct=直连，也可填 http://主机:端口",
        },
        "login_phone": {"type": "str", "label": "登录手机号", "tier": "adv", "section": "账号"},
        "login_password": {
            "type": "str",
            "label": "登录密码",
            "sensitive": True,
            "tier": "adv",
            "section": "账号",
        },
        "use_announcement_style": {
            "type": "bool",
            "label": "公告样式",
            "tier": "adv",
            "section": "行为",
            "hint": "bot 不是域主时保持关闭",
        },
        "app_version": {"type": "str", "label": "app_version", "readonly": True, "tier": "hidden"},
        "device_id": {"type": "str", "label": "device_id", "readonly": True, "tier": "hidden"},
        "person_uid": {"type": "str", "label": "person_uid", "readonly": True, "tier": "hidden"},
        "jwt_token": {"type": "str", "label": "jwt_token", "readonly": True, "tier": "hidden"},
    },
    "onebot": {
        "ws_reverse_url": {
            "type": "str",
            "label": "反向 WS 地址",
            "tier": "basic",
            "prefix": ("ws://", "wss://"),
            "hint": "对端反向 WS 的监听地址，路径由对端决定，如 ws://127.0.0.1:6200/ws",
        },
        "access_token": {
            "type": "str",
            "label": "access_token",
            "sensitive": True,
            "tier": "basic",
            "hint": "要和 OneBot 实现里的 token 相同；两边都留空即不校验",
        },
        "enabled": {"type": "bool", "label": "启用 OneBot v11", "tier": "adv", "section": "连接"},
        "enable_ws_reverse": {
            "type": "bool",
            "label": "启用反向 WebSocket",
            "tier": "adv",
            "section": "连接",
        },
        "ws_reverse_api_url": {
            "type": "str",
            "label": "反向 WS API 地址",
            "tier": "adv",
            "section": "连接",
            "prefix": ("ws://", "wss://"),
            "hint": "仅当对端区分 API/Event 时填写",
        },
        "ws_reverse_event_url": {
            "type": "str",
            "label": "反向 WS Event 地址",
            "tier": "adv",
            "section": "连接",
            "prefix": ("ws://", "wss://"),
        },
        "secret": {
            "type": "str",
            "label": "secret",
            "sensitive": True,
            "tier": "adv",
            "section": "连接",
            "hint": "仅在需要签名校验时填写，一般留空",
        },
        "ws_reverse_reconnect_interval": {
            "type": "float",
            "label": "重连间隔(秒)",
            "tier": "adv",
            "section": "连接",
            "min": 0.5,
            "max": 300,
        },
        "enable_http": {"type": "bool", "label": "本地 HTTP action", "tier": "adv", "section": "本地接入"},
        "enable_ws": {
            "type": "bool",
            "label": "本地正向 WebSocket",
            "tier": "adv",
            "section": "本地接入",
            "hint": "反向桥接不需要，留给主动连过来的 OneBot 客户端",
        },
        "host": {"type": "str", "label": "本地监听地址", "tier": "adv", "section": "本地接入"},
        "port": {
            "type": "int",
            "label": "本地监听端口",
            "tier": "adv",
            "section": "本地接入",
            "min": 1,
            "max": 65535,
        },
        "enable_http_post": {
            "type": "bool",
            "label": "HTTP POST 上报",
            "tier": "adv",
            "section": "本地接入",
        },
        "http_post_urls": {"type": "list", "label": "上报地址列表", "tier": "adv", "section": "本地接入"},
        "send_connect_event": {
            "type": "bool",
            "label": "发送 connect 事件",
            "tier": "adv",
            "section": "推送与心跳",
        },
        "heartbeat_enabled": {"type": "bool", "label": "发送心跳", "tier": "adv", "section": "推送与心跳"},
        "heartbeat_interval": {
            "type": "float",
            "label": "心跳间隔(秒)",
            "tier": "adv",
            "section": "推送与心跳",
            "min": 1,
            "max": 3600,
        },
        "member_list_max": {
            "type": "int",
            "label": "成员列表上限",
            "tier": "adv",
            "section": "推送与心跳",
            "min": 0,
            "max": 100000,
        },
        "enable_area_scoped_group_ban": {
            "type": "bool",
            "label": "禁言作用于整个域",
            "tier": "adv",
            "section": "域语义映射",
        },
        "enable_set_group_kick_as_area_kick": {
            "type": "bool",
            "label": "踢人=移出域",
            "tier": "adv",
            "section": "域语义映射",
        },
        "enable_set_group_leave_as_area_leave": {
            "type": "bool",
            "label": "退群=退出域",
            "tier": "adv",
            "section": "域语义映射",
        },
        "enable_set_group_admin_as_area_role": {
            "type": "bool",
            "label": "设置管理员=域身份组",
            "tier": "adv",
            "section": "域语义映射",
        },
        "group_admin_role_id": {
            "type": "int",
            "label": "管理员身份组 ID",
            "tier": "adv",
            "section": "域语义映射",
            "min": 0,
            "max": 999999,
        },
        "db_path": {
            "type": "str",
            "label": "映射数据库路径",
            "tier": "adv",
            "section": "数据",
            "hint": "存 group_id/self_id 映射，删除会导致对端看到的群号全部变化",
        },
    },
    "voice": {
        "enabled": {"type": "bool", "label": "启用语音 Agent", "tier": "basic"},
        "output_volume": {
            "type": "int",
            "label": "bot 语音音量（%）",
            "tier": "basic",
            "default": 50,
            "min": 10,
            "max": 100,
            "hint": "默认 50%，可调 10-100%。保存立即生效；临时静音请使用语音台的麦克风按钮。",
        },
        "voice_volume_enabled": {
            "type": "bool", "label": "语音控制音量与开闭麦", "tier": "basic", "default": True,
            "hint": "明确对 bot 说调大、调小、设为某个百分比、闭麦或开麦。音量自动保存，闭麦为临时状态；不受回复概率限制。",
        },
        "backend": {
            "type": "select",
            "label": "语音对话后端",
            "tier": "basic",
            "options": [
                {"value": "gemini_live", "label": "Gemini Live（语音进语音出）"},
                {"value": "mimo_cascade", "label": "MiMo 级联（ASR→LLM→TTS）"},
                {"value": "aliyun_deepseek", "label": "阿里语音 + DeepSeek（流式级联）"},
                {"value": "openai_realtime", "label": "OpenAI Realtime（骨架）"},
            ],
            "hint": "选择厂商后出现对应的模型与音色预设",
        },
        "persona": {
            "type": "str",
            "label": "人格提示词",
            "tier": "basic",
            "hint": "语音房里 bot 的说话风格与角色设定",
            "editor": "persona",
        },
        "area": {"type": "str", "label": "默认域 ID", "tier": "basic"},
        "channel": {"type": "str", "label": "默认语音频道 ID", "tier": "basic"},
        "proxy": {
            "type": "select",
            "label": "模型 API 网络代理",
            "tier": "basic",
            "options": [
                {"value": "", "label": "不启用代理"},
                {"value": "clash", "label": "启用代理 127.0.0.1:7890"},
                {"value": "direct", "label": "强制直连"},
            ],
            "allow_custom": True,
            "hint": "选填；模型接口走此代理，也可填 http://主机:端口；更新中心使用独立的更新 Git 代理",
        },
        # ── Gemini Live 厂商预设 ──
        "gemini.api_key": {
            "type": "str",
            "label": "Gemini API Key",
            "sensitive": True,
            "tier": "basic",
            "vendor": "gemini_live",
            "hint": "或环境变量 GEMINI_API_KEY",
        },
        "gemini.base_url": {
            "type": "str",
            "label": "Gemini 接口地址",
            "tier": "basic",
            "vendor": "gemini_live",
            "hint": "Live WebSocket 端点；留空用官方默认，中转/代理可改自定义地址",
            "placeholder": "wss://generativelanguage.googleapis.com/ws/...（默认）",
        },
        "gemini.model": {
            "type": "select",
            "label": "Gemini Live 模型",
            "tier": "basic",
            "vendor": "gemini_live",
            "options": [
                "gemini-2.0-flash-live-001",
                "gemini-2.5-flash-live-preview",
                "gemini-live-2.5-flash-preview",
            ],
            "allow_custom": True,
        },
        "gemini.voice": {
            "type": "select",
            "label": "Gemini 音色",
            "tier": "basic",
            "vendor": "gemini_live",
            "options": [
                {"value": "Puck", "label": "Puck"},
                {"value": "Charon", "label": "Charon"},
                {"value": "Kore", "label": "Kore"},
                {"value": "Fenrir", "label": "Fenrir"},
                {"value": "Aoede", "label": "Aoede"},
                {"value": "Enceladus", "label": "Enceladus"},
                {"value": "Iapetus", "label": "Iapetus"},
                {"value": "Umbriel", "label": "Umbriel"},
            ],
            "allow_custom": True,
            "hint": "下拉预设音色，或选「自定义」填入任意音色名",
        },
        "aliyun_deepseek.ws_url": {
            "type": "str", "label": "阿里语音 WebSocket 地址", "tier": "basic",
            "vendor": "aliyun_deepseek",
            "default": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
            "hint": "密钥仅在服务器 .env.local 配置 DASHSCOPE_API_KEY 和 DEEPSEEK_API_KEY，保存后生效。",
        },
        "aliyun_deepseek.asr_model": {
            "type": "str", "label": "阿里 ASR 模型", "tier": "basic",
            "vendor": "aliyun_deepseek", "default": "qwen-audio-3.1-asr-flash-message",
        },
        "aliyun_deepseek.tts_model": {
            "type": "str", "label": "阿里 TTS 模型", "tier": "basic",
            "vendor": "aliyun_deepseek", "default": "qwen-audio-3.1-tts-flash",
        },
        "aliyun_deepseek.tts_voice": {
            "type": "str", "label": "阿里 TTS 音色", "tier": "basic",
            "vendor": "aliyun_deepseek", "default": "longanhuan_v3.1",
        },
        "aliyun_deepseek.llm_base_url": {
            "type": "str", "label": "DeepSeek 接口地址", "tier": "basic",
            "vendor": "aliyun_deepseek", "default": "https://api.deepseek.com",
        },
        "aliyun_deepseek.llm_model": {
            "type": "str", "label": "DeepSeek 模型", "tier": "basic",
            "vendor": "aliyun_deepseek", "default": "deepseek-flash",
        },
        # ── MiMo 级联厂商预设 ──
        "mimo.api_key": {
            "type": "str",
            "label": "MiMo API Key",
            "sensitive": True,
            "tier": "basic",
            "vendor": "mimo_cascade",
            "hint": "或环境变量 MIMO_API_KEY",
        },
        "mimo.base_url": {
            "type": "str",
            "label": "MiMo 接口地址",
            "tier": "basic",
            "vendor": "mimo_cascade",
        },
        "mimo.asr_model": {
            "type": "select",
            "label": "MiMo ASR 模型",
            "tier": "basic",
            "vendor": "mimo_cascade",
            "options": ["mimo-v2.5-asr"],
            "allow_custom": True,
        },
        "mimo.llm_model": {
            "type": "select",
            "label": "MiMo LLM 模型",
            "tier": "basic",
            "vendor": "mimo_cascade",
            "options": ["mimo-v2.6-flash", "mimo-v2.5-pro"],
            "allow_custom": True,
        },
        "mimo.tts_model": {
            "type": "select",
            "label": "MiMo TTS 模型",
            "tier": "basic",
            "vendor": "mimo_cascade",
            "options": ["mimo-v2.5-tts"],
            "allow_custom": True,
        },
        "mimo.tts_voice": {
            "type": "select",
            "label": "MiMo 音色",
            "tier": "basic",
            "vendor": "mimo_cascade",
            "options": [
                {"value": "冰糖", "label": "冰糖（活泼女声）"},
                {"value": "茉莉", "label": "茉莉（知性女声）"},
                {"value": "苏打", "label": "苏打（阳光男声）"},
                {"value": "白桦", "label": "白桦（沉稳男声）"},
                {"value": "Mia", "label": "Mia"},
                {"value": "Chloe", "label": "Chloe"},
                {"value": "Milo", "label": "Milo"},
                {"value": "Dean", "label": "Dean"},
            ],
            "allow_custom": True,
            "hint": "下拉预设音色，或选「自定义」填入任意音色名",
        },
        # ── OpenAI Realtime 厂商预设 ──
        "openai.api_key": {
            "type": "str",
            "label": "OpenAI API Key",
            "sensitive": True,
            "tier": "basic",
            "vendor": "openai_realtime",
            "hint": "或环境变量 OPENAI_API_KEY",
        },
        "openai.base_url": {
            "type": "str",
            "label": "OpenAI 接口地址",
            "tier": "basic",
            "vendor": "openai_realtime",
            "hint": "兼容 OpenAI 的 API 根地址；留空用官方默认，中转站可改自定义",
            "placeholder": "https://api.openai.com/v1（默认）",
        },
        "openai.realtime_model": {
            "type": "select",
            "label": "OpenAI Realtime 模型",
            "tier": "basic",
            "vendor": "openai_realtime",
            "options": [
                "gpt-4o-mini-realtime-preview",
                "gpt-4o-realtime-preview",
            ],
            "allow_custom": True,
        },
        "openai.voice": {
            "type": "select",
            "label": "OpenAI 音色",
            "tier": "basic",
            "vendor": "openai_realtime",
            "options": [
                {"value": "alloy", "label": "alloy"},
                {"value": "echo", "label": "echo"},
                {"value": "fable", "label": "fable"},
                {"value": "onyx", "label": "onyx"},
                {"value": "nova", "label": "nova"},
                {"value": "shimmer", "label": "shimmer"},
            ],
            "allow_custom": True,
            "hint": "下拉预设音色，或选「自定义」填入任意音色名",
        },
        "reply_probability_percent": {
            "type": "int",
            "label": "语音回复频率（概率 %）",
            "tier": "basic",
            "min": 0,
            "max": 100,
            "default": 30,
            "hint": (
                "默认 30，随机回应约三成发言；0 仅在命中强制关键词时接话，100 每次均允许回复。"
                "保存后对后续回合生效，快捷开口和进退房台词不受限制。"
                "Gemini Live 继续听取对话，按模型回复回合决定是否出声，不保证减少模型用量。"
            ),
        },
        "force_reply_keywords": {
            "type": "list",
            "label": "强制回复关键词列表",
            "tier": "basic",
            "default": [],
            "placeholder": "例如：Ooptra, 机器人, 小欧",
            "hint": (
                "多个词用英文逗号分隔；发言包含任一关键词时绕过回复概率（包括 0%）。"
                "忽略大小写、全半角、空格和标点；以语音转写为准，同音别名可加入列表。"
                "默认留空，保存后对后续回合生效；MiMo 启用关键词时需要先做语音识别。"
                "Gemini 延迟转写最多等待 2 秒；跨回合无法确认归属时仅按概率回复。"
            ),
        },
        "conversation_window_enabled": {
            "type": "bool", "label": "连续对话窗口", "tier": "basic",
            "hint": "叫到关键词后，同一成员可连续追问；换房清除，需先配置强制回复关键词。",
        },
        "conversation_window_seconds": {
            "type": "int", "label": "连续追问窗口 / 秒", "tier": "basic",
            "min": 1, "max": 300,
        },
        "voice_leave_enabled": {
            "type": "bool",
            "label": "语音控制退语音",
            "tier": "basic",
            "default": True,
            "hint": (
                "默认开启，由 AI 判断真人是否要求 bot 退房；所有当前监听的成员均可触发。"
                "支持你出去吧、滚出去等明确对 bot 说的口语，否定和引用不执行。"
                "不受回复概率限制，先播放离场语再退出，按手动退房冷却。"
                "台词复用自动串门的分域退房预设（空列表时采用默认告别）。"
                "MiMo 开启后需要 ASR 和意图判断，可与聊天合并请求，仍可能增加模型用量。"
            ),
        },
        "barge_in": {"type": "bool", "label": "允许抢话打断", "tier": "adv", "section": "行为"},
        "barge_in_hold_ms": {
            "type": "int",
            "label": "抢话门限(毫秒)",
            "tier": "adv",
            "section": "行为",
            "min": 0,
            "max": 3000,
            "hint": (
                "某人要连续说话满这么久才算抢话；说完就断的咳嗽、笑声、键盘声会被忽略。"
                "0 = 关掉门限（一出声就打断，房间一热闹 bot 就说不完整句话）。"
                "房间越吵调越大，600~800 适合人多的时候。"
            ),
        },
        "reply_text_to_channel": {
            "type": "bool",
            "label": "字幕发到文字频道",
            "tier": "adv",
            "section": "行为",
        },
        "silence_ms": {
            "type": "int",
            "label": "静音断句(毫秒)",
            "tier": "adv",
            "section": "音频",
            "min": 200,
            "max": 3000,
        },
        "min_utterance_ms": {
            "type": "int",
            "label": "最短回合(毫秒)",
            "tier": "adv",
            "section": "音频",
            "min": 0,
            "max": 3000,
        },
        "reply_cooldown_ms": {
            "type": "int",
            "label": "回复冷却(毫秒)",
            "tier": "adv",
            "section": "行为",
            "min": 0,
            "max": 10000,
        },
        "sample_rate_in": {"type": "int", "label": "输入采样率", "tier": "adv", "section": "音频", "min": 8000, "max": 48000},
        "sample_rate_out": {"type": "int", "label": "输出采样率", "tier": "adv", "section": "音频", "min": 8000, "max": 48000},
    },
    "voice_api": {
        "enabled": {"type": "bool", "label": "启用语音 API", "tier": "basic"},
        "host": {
            "type": "str",
            "label": "监听地址",
            "tier": "basic",
            "nonempty": True,
            "hint": "127.0.0.1 仅本机",
        },
        "port": {"type": "int", "label": "监听端口", "tier": "basic", "min": 1, "max": 65535},
    },
    "webui": {
        "update_proxy": {
            "type": "str", "label": "更新 Git 代理", "sensitive": True,
            "tier": "adv", "section": "更新网络", "default": "",
            "hint": "部署服务器的 Git 更新出口：留空=系统环境代理，direct=直连，或完整 HTTP/HTTPS/SOCKS 代理 URL；保存后生效",
        },
        "update_mirror": {
            "type": "str", "label": "更新 Git 备用镜像", "tier": "adv",
            "section": "更新网络", "default": "",
            "hint": "完整 HTTPS Git 仓库 URL（支持 Git smart HTTP），留空仅官方源；备用下载必须核对官方提交，不影响 pip/浏览器依赖",
        },
        "host": {
            "type": "str",
            "label": "监听地址",
            "tier": "basic",
            "nonempty": True,
            "hint": "127.0.0.1 仅本机；0.0.0.0 允许局域网访问",
        },
        "port": {"type": "int", "label": "监听端口", "tier": "basic", "min": 1, "max": 65535},
        "token": {
            "type": "str",
            "label": "控制台密码 / 插件 API Token",
            "nonempty": True,
            "max_length": 1024,
            "sensitive": True,
            "tier": "basic",
            "hint": "唯一共享密码。留空不修改；保存后立即生效并要求重新登录，外部插件也需填写同一个新 Token",
        },
        "enabled": {"type": "bool", "label": "启用 Web 控制台", "tier": "adv", "section": "其他"},
        "log_lines": {
            "type": "int",
            "label": "日志默认行数",
            "tier": "adv",
            "section": "其他",
            "min": 50,
            "max": 5000,
        },
    },
}

_RESTART_FREE_FIELDS = {("webui", "token"), ("webui", "log_lines"), ("webui", "update_proxy"), ("webui", "update_mirror")}

# 这些组保存后由 server._hot_reload_voice 在事件循环上真正应用到 VoiceRuntime，
# 故不再标记 restart_required（voice_api 的 host/port 例外，socket 已绑定，
# 由 VoiceRuntime.reload_settings 通过 restart_keys 单独报回来）。
_HOT_RELOAD_GROUPS = {"voice", "voice_api", "auto_visit"}


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _runtime_config():
    return importlib.import_module("config")


def _live_group(group: str) -> dict[str, Any]:
    module = _runtime_config()
    value = getattr(module, GROUP_SOURCES[group], None)
    return value if isinstance(value, dict) else {}


def _split_field_path(field: str) -> list[str]:
    return [part for part in str(field).split(".") if part]


def _nested_get(data: Any, path: list[str]) -> Any:
    node = data
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def schema_payload() -> dict[str, Any]:
    """下发给前端：分组规范 + 当前值。敏感字段不回传明文，自动维护项不下发。"""
    groups: dict[str, Any] = {}
    for group, fields in FIELD_SPECS.items():
        current = _live_group(group)
        entries: dict[str, Any] = {}
        for field, meta in fields.items():
            tier = str(meta.get("tier", "adv"))
            if tier == "hidden" or meta.get("editor") == "persona":
                continue
            entry = {
                "type": meta["type"],
                "label": meta.get("label", field),
                "readonly": bool(meta.get("readonly")),
                "sensitive": bool(meta.get("sensitive")),
                "hint": meta.get("hint", ""),
                "tier": tier,
                "section": meta.get("section", ""),
            }
            for bound in ("min", "max"):
                if bound in meta:
                    entry[bound] = meta[bound]
            for extra in ("options", "vendor", "allow_custom", "placeholder"):
                if extra in meta:
                    entry[extra] = meta[extra]
            value = _nested_get(current, _split_field_path(field))
            if value is None:
                value = meta.get("default")
            if meta.get("readonly"):
                entry["value"] = value
            elif meta.get("sensitive"):
                entry["value"] = None
                entry["is_set"] = bool(value)
            else:
                entry["value"] = list(value) if isinstance(value, (list, tuple)) else value
            entries[field] = entry
        groups[group] = {
            "source": GROUP_SOURCES[group],
            "title": GROUP_META.get(group, {}).get("title", group),
            "desc": GROUP_META.get(group, {}).get("desc", ""),
            "fields": entries,
        }
    return groups


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def _coerce(meta: dict[str, Any], value: Any, field: str) -> Any:
    kind = meta["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{field} 需要布尔值")
    if kind == "int":
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} 需要整数") from exc
        _check_bounds(meta, parsed, field)
        return parsed
    if kind == "float":
        try:
            parsed = float(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} 需要数字") from exc
        _check_bounds(meta, parsed, field)
        return parsed
    if kind == "list":
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in str(value).split(",") if item.strip()]
    text = str(value).strip()
    if len(text) > meta.get("max_length", len(text)):
        raise ValueError(f"{field} 超出最大长度 {meta['max_length']} 字符")
    if kind == "select":
        allowed = _select_values(meta)
        if text in allowed or meta.get("allow_custom") or (not text and not meta.get("nonempty")):
            return text
        raise ValueError(f"{field} 取值不在预设内（可开自定义）")
    if text and meta.get("prefix") and not text.lower().startswith(tuple(meta["prefix"])):
        raise ValueError(f"{field} 必须以 {' 或 '.join(meta['prefix'])} 开头")
    if not text and meta.get("nonempty"):
        raise ValueError(f"{field} 不能为空")
    return text


def _select_values(meta: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for option in meta.get("options") or []:
        if isinstance(option, dict):
            values.add(str(option.get("value", "")))
        else:
            values.add(str(option))
    return values


def _check_bounds(meta: dict[str, Any], value: float, field: str) -> None:
    if "min" in meta and value < meta["min"]:
        raise ValueError(f"{field} 不能小于 {meta['min']}")
    if "max" in meta and value > meta["max"]:
        raise ValueError(f"{field} 不能大于 {meta['max']}")


def _normalize_updates(updates: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(updates, dict):
        raise ValueError("updates 必须是对象")
    normalized: dict[str, dict[str, Any]] = {}
    for group, values in updates.items():
        if group == "auto_visit":
            if not isinstance(values, dict):
                raise ValueError("auto_visit 的值必须是对象")
            # Validate the sparse patch separately. Its null removals must survive
            # normalization until it is merged with the current file under lock.
            merge_auto_visit_patch({}, values)
            if values:
                normalized[group] = copy.deepcopy(values)
            continue
        if group not in FIELD_SPECS:
            raise ValueError(f"未知配置分组: {group}")
        if not isinstance(values, dict):
            raise ValueError(f"{group} 的值必须是对象")
        specs = FIELD_SPECS[group]
        target: dict[str, Any] = {}
        for field, value in values.items():
            meta = specs.get(field)
            if meta is None:
                raise ValueError(f"未知配置项: {group}.{field}")
            if meta.get("readonly"):
                raise ValueError(f"{group}.{field} 是只读项，请通过「Oopz 登录」修改")
            if value is None and meta.get("sensitive"):
                continue
            if group == "voice" and field == "output_volume":
                target[field] = OutputVolume.parse(value).value
            elif group == "webui" and field in {"update_proxy", "update_mirror"}:
                target[field] = validate_network_field(field, value)
            else:
                target[field] = _coerce(meta, value, f"{group}.{field}")
        if target:
            normalized[group] = target
    if not normalized:
        raise ValueError("没有需要保存的改动")
    return normalized


def apply_updates(updates: Any) -> dict[str, Any]:
    """校验并写回 config.py，随后就地更新运行中的字典。"""
    normalized = _normalize_updates(updates)
    change = ConfigurationChange.from_normalized(normalized, sources=GROUP_SOURCES)
    configuration = create_configuration(
        path=CONFIG_PATH, runtime_config=_runtime_config,
        group_mergers={"auto_visit": lambda current, patch: merge_auto_visit_patch(
            {} if current is None else current, patch)},
    )
    receipt = configuration.save_fields(change)
    changed = receipt.changed_fields()
    restart_required = any(
        (group, field) not in _RESTART_FREE_FIELDS and group not in _HOT_RELOAD_GROUPS
        for group, fields in normalized.items()
        for field in fields
    )
    logger.info("配置已保存: %s", changed)
    return {"changed": changed, "restart_required": restart_required}


def apply_auto_visit_updates(patch: Any) -> dict[str, Any]:
    """Save a validated sparse auto-visit patch and synchronize live config."""
    return apply_updates({"auto_visit": patch})


__all__ = [
    "CONFIG_PATH", "FIELD_SPECS", "GROUP_SOURCES", "apply_auto_visit_updates",
    "apply_updates", "schema_payload",
]
