"""Ooptra VOICE_API —— 与 astrbot_plugin_ooptra 契约对齐。

插件契约（默认 api_base=http://127.0.0.1:3090，无 /api 前缀）：

    GET  /health
    GET  /voice/status
    GET  /voice/members?area=&channel=
    GET  /voice/channels?area=
    POST /voice/join    {"area","channel"}
    POST /voice/leave
    POST /voice/speak   {"text"}
    Authorization: Bearer <token>   # 可选

本模块是语音 HTTP 契约的**唯一实现**，两个入口共用：

  * WebUI（3090）—— ``mount_voice_routes``，额外挂 /api/* 历史别名
  * 独立 VOICE_API 端口（3091）—— ``build_voice_routes``，见 app/http.py

不要在任何地方另写一份 handler：3091 曾经自带一套手写路由，结果与 3090 分叉
（缺 ``/voice/channels``、``/voice/members`` 因 pydantic 模型不可序列化而 500）。

响应形态（扁平，插件 format_status / format_members 直接读）：

    status:  {ok, joined, area, channel, state, ...}
    members: {ok, count, members:[{uid,name,mic,speaker,m,hm,mic_muted,speaker_muted,live}],
              area, channel, live_state_received, channel_counts}
             mic/speaker: true=开，false=闭，null=未知
             m/hm:        1=闭麦/闭听，0=正常，null=未知
             mic_muted/speaker_muted: true=闭，false=开，null=未知（WebUI 读这两个）
             live:        该行的静音状态是否来自实时广播
             live_state_received: 本房累计收到的静音状态广播条数
             0 表示数据源没在广播（不是「都没闭麦」），此时静音列只能显示未知
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from aiohttp import web

from context.models import BindingRequest, PollRequest, RpcReceipt

logger = logging.getLogger(__name__)

# 昵称补全的超时：宁可显示短 ID，也不让 /voice/members 卡住
_NAME_TIMEOUT = 3.0


# ----------------------------------------------------------------------
# 成员归一
# ----------------------------------------------------------------------


def _field(row: Any, *names: str, default: Any = None) -> Any:
    """从 pydantic 模型或原始 dict 里按候选字段名取值，取不到返回 default。"""
    for name in names:
        value = row.get(name) if isinstance(row, dict) else getattr(row, name, None)
        if value is not None:
            return value
    return default


def _muted(row: Any, muted_key: str, flag_key: str, state_key: str) -> bool | None:
    """归一静音状态。返回 True=已闭，False=开，None=未知（不猜）。"""
    muted = _field(row, muted_key)
    if muted is not None:
        return bool(muted)
    flag = _field(row, flag_key)  # m / hm：1=闭，0=开
    if flag is not None:
        try:
            return int(flag) == 1
        except (TypeError, ValueError):
            pass
    state = _field(row, state_key)  # mic / speaker：true=开
    if state is not None:
        return not bool(state)
    return None


def _live_muted(live: Any, key: str) -> bool | None:
    """从实时状态里取 m / hm（1=闭，0=开）；取不到返回 None。"""
    value = _field(live, key) if live else None
    if value is None:
        return None
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return None


def normalize_member(row: Any, name: str = "", live: Any = None) -> dict[str, Any]:
    """把 SDK 模型或原始 dict 归一成插件与 WebUI 都能读的成员结构。

    ``mic``/``speaker``/``m``/``hm`` 给插件读，``mic_muted``/``speaker_muted``
    给 WebUI 读。

    静音状态有两个来源，取到哪个用哪个：
      * ``row`` 自带字段 —— 当前 Oopz 的 membersByChannels **不返回**
      * ``live``：成员自己用 Agora stream message 广播的 {m, hm}（更实时，优先）
    都没有时一律为 ``None``（未知），不伪造成「开麦」。
    """
    uid = str(_field(row, "uid", "pid", "person_uid", "user_id", default="") or "")
    resolved = name or str(_field(row, "name", "nickname", "user_name", default="") or "")

    live_mic = _live_muted(live, "m")
    live_sp = _live_muted(live, "hm")
    mic_muted = live_mic if live_mic is not None else _muted(row, "mic_muted", "m", "mic")
    sp_muted = live_sp if live_sp is not None else _muted(row, "speaker_muted", "hm", "speaker")

    return {
        "uid": uid,
        "name": resolved,
        # 插件首选 mic/speaker（true=开）
        "mic": None if mic_muted is None else (not mic_muted),
        "speaker": None if sp_muted is None else (not sp_muted),
        # 兼容 Oopz 标志（1=闭）
        "m": None if mic_muted is None else (1 if mic_muted else 0),
        "hm": None if sp_muted is None else (1 if sp_muted else 0),
        # WebUI 读这两个
        "mic_muted": mic_muted,
        "speaker_muted": sp_muted,
        "is_bot": bool(_field(row, "is_bot", "isBot", default=False) or False),
        # 是否来自实时广播。WebUI 用它区分「实时状态」与「纯 REST（未知）」
        "live": live_mic is not None or live_sp is not None,
    }


async def _member_names(uids: list[str]) -> dict[str, str]:
    """批量补全昵称，失败/超时则回退 NameResolver 的短 ID。"""
    unique = [uid for uid in dict.fromkeys(uids) if uid]
    if not unique:
        return {}
    try:
        from channels import name_directory

        resolver = name_directory()
    except Exception:
        logger.debug("name resolver unavailable", exc_info=True)
        return {}
    try:
        await asyncio.wait_for(resolver.ensure_users(unique), timeout=_NAME_TIMEOUT)
    except Exception as exc:
        logger.debug("resolve member names failed: %s", exc)
    names: dict[str, str] = {}
    for uid in unique:
        try:
            names[uid] = resolver.user_cached(uid)
        except Exception:
            names[uid] = ""
    return names


def _channel_member_map(members_raw: Any) -> dict[str, list[Any]]:
    """把 get_voice_channel_members 结果归一成 {channel_id: [member, ...]}。"""
    if members_raw is None:
        return {}
    grouped = getattr(members_raw, "channel_members", None)
    if isinstance(grouped, dict):
        return {str(k): list(v or []) for k, v in grouped.items()}
    if isinstance(members_raw, dict):
        src = members_raw.get("channelMembers") or members_raw.get("channel_members")
        if isinstance(src, dict):
            return {str(k): list(v or []) for k, v in src.items()}
    return {}


def _default_target() -> tuple[str, str]:
    """读取 OOPZ_CONFIG 里「设为默认」的域/频道。"""
    try:
        import config as runtime_config

        return (
            str(runtime_config.OOPZ_CONFIG.get("default_area") or ""),
            str(runtime_config.OOPZ_CONFIG.get("default_channel") or ""),
        )
    except Exception:
        return "", ""


# ----------------------------------------------------------------------
# 路由构造
# ----------------------------------------------------------------------


def build_voice_routes(
    voice_runtime: Any,
    *,
    with_api_alias: bool = False,
    wrap: Callable[[Any], Any] | None = None,
) -> list[Any]:
    """构造语音契约路由。

    ``voice_runtime`` 需提供 ``.agent``；handler 在**每次请求时**重新读取
    ``voice_runtime.agent``，这样热重载替换 agent 后无需重新挂载路由。

    ``wrap`` 用于包一层鉴权/异常兜底（独立 VOICE_API 端口用）。
    """

    def current_agent() -> Any:
        return voice_runtime.agent

    def ok(payload: dict[str, Any] | None = None) -> web.Response:
        data: dict[str, Any] = {"ok": True}
        if payload:
            data.update(payload)
        return web.json_response(data)

    def err(message: str, status: int = 400) -> web.Response:
        return web.json_response({"ok": False, "error": message}, status=status)

    async def read_json(request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise web.HTTPBadRequest(text="请求体必须是有效 JSON 对象") from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="请求体必须是 JSON 对象")
        return data

    # ── 插件契约：扁平 status ──
    def status_payload(agent: Any) -> dict[str, Any]:
        st = agent.status() or {}
        joined = bool(st.get("joined"))
        state = "joined" if joined else "idle"
        if st.get("speaking"):
            state = "playing"
        default_area, default_channel = _default_target()
        if not default_area and not default_channel:
            default_area = agent.settings.area
            default_channel = agent.settings.channel
        return {
            "joined": joined,
            "area": st.get("area") or "",
            "channel": st.get("channel") or "",
            "state": state,
            "backend": st.get("backend") or "",
            "mode": st.get("mode") or "",
            "enabled": bool(st.get("enabled")),
            "turns": int(st.get("turns") or 0),
            "last_reply": st.get("last_reply") or "",
            "last_user_text": st.get("last_user_text") or "",
            # 插件侧默认目标（WebUI「设为默认」写入 OOPZ_CONFIG）
            "default_area": default_area or agent._area or "",
            "default_channel": default_channel or agent._channel or "",
            # WebUI 兼容
            "status": st,
            "join_source": st.get("join_source", ""),
            "astrbot_context": st.get("astrbot_context", {}),
        }

    from dialogue import create_preview_service
    preview_service = create_preview_service()

    async def voice_diagnostics(request: web.Request) -> web.Response:
        import base64
        import binascii

        from app.diagnostics import diagnose
        try:
            body = await request.json() if request.can_read_body else {}
            if not isinstance(body, dict):
                return err("自检参数必须是 JSON 对象")
            source = body.get("audio_wav_base64")
            audio = None
            if source is not None:
                if not isinstance(source, str) or not source or len(source) > 2_796_204:
                    return err("请上传不超过 2 MiB 的短句 WAV")
                audio = base64.b64decode(source, validate=True)
                if len(audio) > 2 * 1024 * 1024:
                    return err("请上传不超过 2 MiB 的短句 WAV")
            return ok(await diagnose(current_agent(), network=body.get("network") is True, audio_wav=audio))
        except web.HTTPRequestEntityTooLarge:
            return err("请上传不超过 2 MiB 的短句 WAV", 413)
        except (ValueError, binascii.Error):
            return err("音频或自检参数无效，请上传短句 WAV")

    async def voice_stop(_request: web.Request) -> web.Response:
        try:
            await current_agent().stop_current_reply()
            return ok({"speaking": False})
        except Exception:
            logger.exception("voice stop failed")
            return err("停止回复失败，请检查当前语音状态", 503)

    async def voice_preview(request: web.Request) -> web.Response:
        body = await read_json(request)
        try:
            result = await preview_service.generate(
                current_agent().settings, str(body.get("kind") or "voice"),
                str(body.get("text") or ""), str(body.get("voice") or ""))
            return ok(result)
        except (ValueError, asyncio.TimeoutError) as exc:
            return err(str(exc) or "试听超时，请检查模型连接")
        except Exception as exc:
            return err(f"{type(exc).__name__}：试听生成失败，请检查模型与音色配置")

    async def voice_preview_prompts(request: web.Request) -> web.Response:
        from behavior import parse_auto_visit_config, saved_prompts
        controller = getattr(voice_runtime, "auto_visit", None)
        config = controller.config if controller is not None else parse_auto_visit_config(None)
        return ok({"prompts": saved_prompts(config, request.query.get("kind", ""), request.query.get("area", ""))})

    async def health(_request: web.Request) -> web.Response:
        agent = current_agent()
        return ok(
            {
                "service": "ooptra-voice-api",
                "version": _ooptra_version(),
                "enabled": bool(agent.settings.enabled),
            }
        )

    async def voice_status(_request: web.Request) -> web.Response:
        payload = status_payload(current_agent())
        controller = getattr(voice_runtime, "auto_visit", None)
        if controller is not None:
            payload["auto_visit"] = controller.status()
        return ok(payload)

    def visit_payload() -> dict[str, Any]:
        controller = getattr(voice_runtime, "auto_visit", None)
        if controller is None:
            raise web.HTTPServiceUnavailable(reason="自动串门控制器尚未就绪")
        return {"config": asdict(controller.config), "status": controller.status()}

    async def auto_visit_get(_request: web.Request) -> web.Response:
        return ok(visit_payload())

    async def auto_visit_config(request: web.Request) -> web.Response:
        body = await read_json(request)
        if not isinstance(body.get("updates"), dict):
            return err("updates 必须是对象")
        # Check availability before touching config.py.
        visit_payload()
        from webui.config_editor import apply_auto_visit_updates

        try:
            result = await asyncio.to_thread(apply_auto_visit_updates, body["updates"])
        except ValueError as exc:
            return err(str(exc))
        except Exception as exc:
            logger.exception("auto visit config save failed")
            return err(str(exc), 500)
        try:
            applied = await voice_runtime.reload_settings()
        except Exception as exc:
            await voice_runtime.auto_visit.pause()
            return err(f"配置已保存，但应用失败，自动加入已暂停：{exc}", 503)
        return ok({**visit_payload(), **result, "notes": applied.get("notes", [])})

    async def auto_visit_pause(_request: web.Request) -> web.Response:
        visit_payload()
        await voice_runtime.auto_visit.pause()
        return ok({"status": voice_runtime.auto_visit.status()})

    async def auto_visit_resume(_request: web.Request) -> web.Response:
        visit_payload()
        try:
            await voice_runtime.auto_visit.resume()
        except Exception as exc:
            return err(str(exc), 409)
        return ok({"status": voice_runtime.auto_visit.status()})

    async def voice_join(request: web.Request) -> web.Response:
        agent = current_agent()
        body = await read_json(request)
        area = str(body.get("area") or request.query.get("area") or "")
        channel = str(body.get("channel") or request.query.get("channel") or "")
        try:
            result = await agent.join(area, channel)
            if result.get("ok") is False:
                message = str(result.get("error") or "进语音失败")
                logger.warning("voice join failed: %s", message)
                return err(message, 503)
        except Exception as exc:
            logger.warning("voice join failed: %s", exc)
            return err(str(exc) or "进语音失败", 503)
        return ok(
            {
                "joined": True,
                "area": result.get("area") or area,
                "channel": result.get("channel") or channel,
                "mode": result.get("mode") or "",
            }
        )

    async def voice_leave(_request: web.Request) -> web.Response:
        agent = current_agent()
        try:
            result = await agent.leave()
            if result.get("ok") is False:
                message = str(result.get("error") or "退语音失败")
                logger.warning("voice leave failed: %s", message)
                return err(message, 503)
        except Exception as exc:
            logger.warning("voice leave failed: %s", exc)
            return err(str(exc) or "退语音失败", 503)
        return ok({"joined": False})

    async def voice_members(request: web.Request) -> web.Response:
        agent = current_agent()
        directory = agent.directory
        if not directory.available:
            return err("Oopz bot 尚未就绪", 503)
        area = str(request.query.get("area") or agent._area or "")
        if not area:
            return err("缺少 area 参数")
        channel = str(request.query.get("channel") or "").strip()
        try:
            members_raw = await directory.member_data(area)
        except Exception as exc:
            logger.warning("get voice members failed: %s", exc)
            return err(f"获取语音成员失败：{exc}", 502)

        grouped = _channel_member_map(members_raw)
        if channel:
            raw_list = grouped.get(channel, [])
        else:
            # 未指定频道时汇总整个域；同时回传分频道人数
            raw_list = [m for rows in grouped.values() for m in rows]

        rows = [row for row in (raw_list or []) if row is not None]
        names = await _member_names(
            [str(_field(row, "uid", "pid", "person_uid", "user_id", default="") or "") for row in rows]
        )
        # 实时静音状态：成员用 Agora stream message 自己广播的那份（REST 不提供）
        live_states, live_rx = directory.live_states()
        items = []
        live_hits = 0
        for row in rows:
            uid = str(_field(row, "uid", default="") or "")
            live = live_states.get(uid)
            if live is not None:
                live_hits += 1
            items.append(normalize_member(row, names.get(uid, ""), live))

        return ok(
            {
                "count": len(items),
                "members": items,
                "area": area,
                "channel": channel or agent._channel or "",
                # 0 = 本房一次广播都没收到 → 静音状态无法得知，不是「都没闭麦」
                "live_state_received": live_rx,
                # 拿到实时状态的人数。广播是「一个成员进房/改状态时发一条」，
                # 所以这个数通常小于 count —— 差集是没广播过的人，不是我们没收到。
                "live_members": live_hits,
                "channel_counts": {
                    cid: len(
                        [
                            m
                            for m in rows_in_channel
                            if not bool(
                                _field(m, "is_bot", "isBot", default=False) or False
                            )
                        ]
                    )
                    for cid, rows_in_channel in grouped.items()
                },
            }
        )

    async def voice_channels(request: web.Request) -> web.Response:
        """列出域内语音频道及在线人数，供插件按域汇总、按名字找频道。"""
        agent = current_agent()
        directory = agent.directory
        if not directory.available:
            return err("Oopz bot 尚未就绪", 503)
        area = str(request.query.get("area") or agent._area or "").strip()
        if not area:
            return err("缺少 area 参数")

        names: dict[str, str] = {}
        try:
            for channel in await directory.named_channels(area):
                names[channel["id"]] = channel["name"]
        except Exception as exc:
            logger.warning("list area channels failed: %s", exc)

        counts: dict[str, int] = {}
        try:
            members_raw = await directory.member_data(area)
            for cid, members in _channel_member_map(members_raw).items():
                counts[cid] = len(
                    [
                        m
                        for m in members
                        if not bool(_field(m, "is_bot", "isBot", default=False) or False)
                    ]
                )
        except Exception as exc:
            logger.warning("count voice members failed: %s", exc)

        channels = [
            {"id": cid, "name": names.get(cid) or cid, "count": int(counts.get(cid) or 0)}
            for cid in sorted(
                set(names) | set(counts),
                key=lambda x: (-counts.get(x, 0), names.get(x) or x),
            )
        ]
        default_area, default_channel = _default_target()
        return ok(
            {
                "area": area,
                "channels": channels,
                "default_area": default_area or agent._area or "",
                "default_channel": default_channel or agent._channel or "",
            }
        )

    async def voice_speak(request: web.Request) -> web.Response:
        agent = current_agent()
        body = await read_json(request)
        text = str(body.get("text") or "")
        try:
            result = await agent.speak_text(text)
        except Exception as exc:
            logger.warning("voice speak failed: %s", exc)
            return err(str(exc) or "speak failed", 502)
        if not result.get("ok"):
            return err(str(result.get("error") or "speak failed"))
        return ok(result)

    async def voice_output(request: web.Request) -> web.Response:
        body = await read_json(request)
        if not body or set(body) - {"volume", "muted"}:
            return err("请提供 volume 或 muted，不支持其他字段")
        if "volume" in body and (type(body["volume"]) is not int or not 10 <= body["volume"] <= 100):
            return err("音量必须为 10-100 的整数")
        if "muted" in body and type(body["muted"]) is not bool:
            return err("muted 必须为布尔值")
        result = await current_agent().set_output_controls(**body)
        if not result.get("ok"):
            return err(str(result.get("error") or "语音输出调整失败"), 502)
        return ok(result)

    # ── 域/频道（WebUI 用）──
    async def oopz_areas(_request: web.Request) -> web.Response:
        agent = current_agent()
        directory = agent.directory
        if not directory.available:
            return err("Oopz bot 尚未就绪", 503)
        items = list(await directory.named_areas())
        default_area, default_channel = _default_target()
        return ok(
            {
                "areas": items,
                "default_area": default_area or agent._area,
                "default_channel": default_channel or agent._channel,
            }
        )

    async def oopz_channels(request: web.Request) -> web.Response:
        agent = current_agent()
        directory = agent.directory
        if not directory.available:
            return err("Oopz bot 尚未就绪", 503)
        area = str(request.query.get("area") or "").strip()
        if not area:
            return err("缺少 area 参数")
        channels = list(await directory.named_channels(area))
        return ok({"area": area, "channels": channels})

    # ── 人格 / 记忆 ──
    async def persona_get(_request: web.Request) -> web.Response:
        agent = current_agent()
        bridge = getattr(agent, "context_bridge", None)
        shared = bridge.status() if bridge else {}
        return ok({"persona": agent.settings.persona,
                   "effective_persona": bridge.persona if bridge and bridge.active else agent.settings.persona,
                   "astrbot_context": shared})

    async def persona_put(request: web.Request) -> web.Response:
        bridge = getattr(current_agent(), "context_bridge", None)
        if bridge is not None and bridge.active:
            return err("当前人格由 AstrBot 管理，请在配套插件选择人格；停用同步并刷新上下文后可编辑本地人格", 409)
        agent = current_agent()
        body = await read_json(request)
        persona = body.get("persona")
        if not isinstance(persona, str) or not persona.strip():
            return err("persona 不能为空")
        from webui import config_editor
        try:
            await asyncio.to_thread(config_editor.apply_updates, {"voice": {"persona": persona}})
        except Exception:
            logger.exception("人格配置保存失败")
            return err("人格保存失败，请检查配置文件权限后重试", 500)
        agent.update_persona(persona.strip())
        notes = await agent.refresh(["persona"])
        return ok({"persona": agent.settings.persona, "notes": notes})

    async def memory_get(request: web.Request) -> web.Response:
        agent = current_agent()
        user_key = request.query.get("user_key", "")
        limit_raw = request.query.get("limit")
        limit = int(limit_raw) if limit_raw and str(limit_raw).isdigit() else None
        return ok(
            {
                "messages": agent.memory.as_messages(user_key=user_key, limit=limit),
                "persona": agent.settings.persona,
            }
        )

    async def memory_post(request: web.Request) -> web.Response:
        agent = current_agent()
        body = await read_json(request)
        role = str(body.get("role") or "user")
        content = str(body.get("content") or "")
        if role not in {"user", "assistant", "system"}:
            return err("role 无效")
        if not content.strip():
            return err("content 不能为空")
        row = agent.memory.append(
            role,
            content,
            user_key=str(body.get("user_key") or ""),
            channel_key=str(body.get("channel_key") or ""),
        )
        return ok({"turn": row})

    async def memory_delete(request: web.Request) -> web.Response:
        agent = current_agent()
        body = await read_json(request)
        removed = agent.memory.clear(user_key=str(body.get("user_key") or ""))
        return ok({"removed": removed})

    # ── 路由：插件契约 + /api 别名 ──
    async def context_poll(request: web.Request) -> web.Response:
        client_id = request.query.get("client_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", client_id):
            return err("缺少有效桥接实例标识")
        try:
            result = await current_agent().context_bridge.poll_request(PollRequest(client_id))
            if result.status == "peer_conflict":
                return err("桥接实例已变化", 409)
            events = result.events()
        except ValueError as exc:
            return err(str(exc), 409)
        return ok({"protocol": 1, "events": events})

    async def context_result(request: web.Request) -> web.Response:
        body = await read_json(request)
        try:
            receipt = RpcReceipt.parse(body)
        except ValueError:
            return err("桥接结果格式不正确")
        result = current_agent().context_bridge.resolve_receipt(receipt)
        if result.status == "rejected":
            return err("桥接实例已变化", 409)
        return ok({"accepted": result.accepted})

    async def context_refresh(_request: web.Request) -> web.Response:
        return ok(await current_agent().refresh_astrbot_context())

    async def context_text_channels(request: web.Request) -> web.Response:
        from channels.api import TransportFailure
        area = request.query.get("area", "").strip()
        if not area or len(area) > 256:
            return err("请选择 Oopz 文字频道所在域")
        try:
            return ok(await current_agent().directory.text_channels(area))
        except TransportFailure:
            return err("Oopz OneBot v11 桥接未就绪，请先启用文字桥接并连接 AstrBot。", 503)

    async def context_bindings(request: web.Request) -> web.Response:
        bridge = current_agent().context_bridge
        if not bridge.connected:
            return err("插件未连接。请启用 astrbot_plugin_ooptra 2.0.0 或以上版本，并检查 api_base 与 api_token。", 503)
        body = {"action": "list"} if request.method == "GET" else await read_json(request)
        try:
            binding = BindingRequest.parse(body)
        except ValueError:
            return err("不支持的绑定操作")
        result = await bridge.bindings(binding)
        return ok(result) if result.get("ok") is True else err(result.get("error", "绑定操作失败"))

    pairs = [
        ("GET", "/voice/astrbot/events", context_poll),
        ("POST", "/voice/astrbot/result", context_result),
        ("POST", "/voice/astrbot/refresh", context_refresh),
        ("GET", "/voice/astrbot/text-channels", context_text_channels),
        ("GET", "/voice/astrbot/bindings", context_bindings),
        ("POST", "/voice/astrbot/bindings", context_bindings),
        ("GET", "/health", health),
        ("GET", "/voice/status", voice_status),
        ("POST", "/voice/diagnostics", voice_diagnostics),
        ("POST", "/voice/preview", voice_preview),
        ("GET", "/voice/preview/prompts", voice_preview_prompts),
        ("GET", "/voice/auto-visit", auto_visit_get),
        ("POST", "/voice/auto-visit/config", auto_visit_config),
        ("POST", "/voice/auto-visit/pause", auto_visit_pause),
        ("POST", "/voice/auto-visit/resume", auto_visit_resume),
        ("GET", "/voice/members", voice_members),
        ("GET", "/voice/channels", voice_channels),
        ("POST", "/voice/join", voice_join),
        ("POST", "/voice/leave", voice_leave),
        ("POST", "/voice/stop", voice_stop),
        ("POST", "/voice/speak", voice_speak),
        ("POST", "/voice/output", voice_output),
        ("GET", "/oopz/areas", oopz_areas),
        ("GET", "/oopz/channels", oopz_channels),
        ("GET", "/persona", persona_get),
        ("PUT", "/persona", persona_put),
        ("GET", "/memory", memory_get),
        ("POST", "/memory", memory_post),
        ("DELETE", "/memory", memory_delete),
    ]
    routes: list[Any] = []
    for method, path, handler in pairs:
        if wrap is not None:
            handler = wrap(handler)
        routes.append(web.route(method, path, handler))
        if with_api_alias:
            # WebUI 历史路径 /api/*
            routes.append(web.route(method, "/api" + path, handler))
    return routes


def _ooptra_version() -> str:
    try:
        from core.version import __version__

        return str(__version__)
    except Exception:
        return ""


def mount_voice_routes(app: web.Application, voice_runtime: Any) -> None:
    """挂到 WebUI app 上：插件契约路径 + /api/* 历史别名。"""
    routes = build_voice_routes(voice_runtime, with_api_alias=True)
    app.add_routes(routes)
    logger.debug("voice routes mounted (%d routes, plugin contract + /api aliases)", len(routes))
