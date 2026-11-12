"""Oopz 房间与音频公开端口适配；不拥有房间或模型状态。"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

from channels.api import AudioOutput, TransportFailure
from channels.models import (
    AreaMembership,
    AudioPushResult,
    JoinedAreas,
    PlatformMember,
    RoomMembership,
    RoomTarget,
)

T = TypeVar("T")


async def _external(work: Awaitable[T]) -> T:
    try:
        return await work
    except Exception:
        raise TransportFailure("外部传输未确认") from None


class OopzRoomTransport:
    def __init__(self, bot: Any, output: Callable[[], AudioOutput | None], controls=None) -> None:
        self._bot = bot
        self._controls = controls
        self._output = output
        self._bound_output: AudioOutput | None = None

    async def join(self, target: RoomTarget) -> str:
        if self._bot is None:
            raise TransportFailure("Oopz bot 尚未就绪")
        self._bound_output = self._output()
        sign = await _external(self._bot.voice.join(area=target.area, channel=target.channel))
        return str(getattr(sign, "rtc_channel_name", ""))

    async def leave(self) -> None:
        if self._bot is not None:
            await _external(self._bot.voice.leave())

    async def retire_output(self) -> None:
        output = self._bound_output
        if output is not None:
            result = await _external(output.stop_tts())
            if isinstance(result, dict) and result.get("ok") is False:
                raise TransportFailure("输出收尾未确认")

    async def text_target_exists(self, target: RoomTarget) -> bool:
        if self._bot is None:
            return False
        groups = await _external(self._bot.areas.get_area_channels(target.area))
        for group in groups or []:
            for channel in getattr(group, "channels", None) or []:
                identifier = str(getattr(channel, "channel_id", "") or "")
                kind = getattr(channel, "channel_type", "")
                kind = str(getattr(kind, "value", kind)).upper()
                if identifier == target.channel:
                    return kind in {"TEXT", "CHAT"}
        return False

    async def send_text(self, target: RoomTarget, text: str) -> str:
        if self._bot is None:
            raise TransportFailure("text_unavailable")
        # SDK API is send_message(*texts, area=..., channel=...), not send().
        result = await _external(self._bot.messages.send_message(text, area=target.area, channel=target.channel))
        message_id = getattr(result, "message_id", None)
        if not isinstance(message_id, str) or not message_id.strip():
            raise TransportFailure("text_unconfirmed")
        return message_id

    async def apply_output(self, volume, muted):
        controls = self._controls() if self._controls else None
        if controls is None:
            return AudioPushResult(False, "output_unavailable")
        result = await controls.set(volume=volume, muted=muted)
        return AudioPushResult(result.get("ok") is True, "" if result.get("ok") else "apply_failed")

    async def wait_playback(self, timeout: float) -> AudioPushResult:
        output = self._bound_output
        drain = getattr(output, "wait_tts_complete", None)
        if drain is None:
            return AudioPushResult(False, "confirmation_unavailable")
        result = await _external(drain(timeout))
        controls = self._controls() if self._controls else None
        if controls is not None and controls.muted:
            return AudioPushResult(False, "output_muted")
        if not isinstance(result, dict) or result.get("ok") is not True:
            return AudioPushResult(False, "drain_failed")
        return AudioPushResult(True)

    async def push_audio(self, pcm: bytes, rate: int, *, finish: bool = False) -> AudioPushResult:
        output = self._bound_output
        if output is None:
            return AudioPushResult(False, "output_unavailable")
        controls = self._controls() if self._controls else None
        if controls is not None and controls.muted:
            return AudioPushResult(False, "output_muted")
        result = await _external(output.push_tts_pcm(pcm, rate, finish=finish))
        if not isinstance(result, dict) or result.get("ok") is not True:
            return AudioPushResult(False, "transport_failed")
        return AudioPushResult(True)


def _field(value: object, name: str, default: object = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _identifier(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value
    if type(value) is int:
        return str(value)
    raise ValueError("平台标识无效")


def _bot_flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int)):
        normalized = str(value).lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    return None


class OopzDirectory:
    """仅此处了解 SDK 返回的属性/字典别名和平台缺省值。"""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    @property
    def available(self) -> bool:
        return self._bot is not None

    async def member_data(self, area: str):
        if self._bot is None:
            raise TransportFailure("Oopz bot 尚未就绪")
        raw = await asyncio.wait_for(self._bot.channels.get_voice_channel_members(area=area), 10)
        return _plain(raw)

    def live_states(self):
        import copy
        try:
            voice = getattr(self._bot, "voice", None)
            states = voice.voice_states() if voice is not None else {}
            received = int(getattr(voice, "voice_state_received", 0) or 0)
        except Exception:
            return {}, 0
        if not isinstance(states, dict):
            return {}, received
        return copy.deepcopy({str(uid): row for uid, row in states.items() if isinstance(row, dict)}), received

    async def named_areas(self):
        if self._bot is None:
            raise TransportFailure("Oopz bot 尚未就绪")
        rows = await asyncio.wait_for(self._bot.areas.get_joined_areas(), 10)
        return tuple({"id": str(getattr(row, "area_id", "") or getattr(row, "id", "") or ""),
                      "name": str(getattr(row, "name", "") or "")} for row in rows or [])

    async def named_channels(self, area: str):
        if self._bot is None:
            raise TransportFailure("Oopz bot 尚未就绪")
        groups = await asyncio.wait_for(self._bot.areas.get_area_channels(area), 10)
        result = []
        for group in groups or []:
            for channel in getattr(group, "channels", None) or []:
                kind = getattr(channel, "channel_type", "")
                kind = str(getattr(kind, "value", kind)).upper()
                if kind and kind not in {"VOICE", "AUDIO"}:
                    continue
                result.append({"id": str(getattr(channel, "channel_id", "") or getattr(channel, "id", "") or ""),
                               "name": str(getattr(channel, "name", "") or ""), "type": kind or "VOICE"})
        return tuple(result)

    async def text_channels(self, area: str) -> dict:
        from channels._internal.adapters.onebot_v11.sdk_integration import find_sdk_onebot_v11
        from oopz_sdk.adapters.onebot.v11.types import make_group_source
        bot = self._bot
        adapter = find_sdk_onebot_v11(bot)
        if bot is None or adapter is None:
            raise TransportFailure("Oopz OneBot v11 桥接未就绪")
        groups = await asyncio.wait_for(bot.areas.get_area_channels(area), timeout=10)
        rows = []
        for group in groups or []:
            for channel in getattr(group, "channels", None) or []:
                kind = getattr(channel, "channel_type", "")
                if str(getattr(kind, "value", kind)).upper() not in {"TEXT", "CHAT"}:
                    continue
                cid = str(getattr(channel, "channel_id", "") or "")
                if not cid:
                    continue
                mapped = adapter.ids.createId(make_group_source(area=area, channel=cid))
                rows.append({"id": cid, "name": str(getattr(channel, "name", "") or cid),
                             "group_id": str(mapped.number), "area": area})
        return {"channels": rows, "bot_id": str(adapter.self_id)}

    async def joined_areas(self) -> JoinedAreas:
        if self._bot is None:
            return JoinedAreas("unavailable")
        try:
            raw = await asyncio.wait_for(_external(self._bot.areas.get_joined_areas()), 10)
        except (TransportFailure, asyncio.TimeoutError):
            return JoinedAreas("unavailable")
        try:
            if not isinstance(raw, (list, tuple)):
                raise ValueError("域列表无效")
            ids = tuple(_identifier(_field(row, "area_id", _field(row, "id"))) for row in raw)
            return JoinedAreas("confirmed", ids)
        except ValueError:
            return JoinedAreas("invalid_response")

    async def voice_members(self, area: str) -> AreaMembership:
        if not isinstance(area, str) or not area.strip():
            raise ValueError("域 ID 必须是非空字符串")
        if self._bot is None:
            return AreaMembership(area, "unavailable")
        try:
            raw = await asyncio.wait_for(
                _external(self._bot.channels.get_voice_channel_members(area=area)), 10)
        except (TransportFailure, asyncio.TimeoutError):
            return AreaMembership(area, "unavailable")
        try:
            if _field(raw, "source_validated", True) is not True:
                raise ValueError("成员来源格式无效")
            grouped = _field(raw, "channel_members")
            if grouped is None:
                grouped = _field(raw, "channelMembers")
            if not isinstance(grouped, Mapping):
                raise ValueError("频道列表无效")
            own_uid = _field(_field(self._bot, "config"), "person_uid", "")
            own_uid = str(own_uid) if isinstance(own_uid, str) or type(own_uid) is int else ""
            rooms = []
            for channel, raw_members in grouped.items():
                target = RoomTarget(area, _identifier(channel))
                if not isinstance(raw_members, list):
                    raise ValueError("成员列表无效")
                members = []
                for row in raw_members:
                    if _field(row, "source_validated", True) is not True:
                        raise ValueError("成员来源格式无效")
                    if not isinstance(row, Mapping) and not hasattr(row, "uid"):
                        raise ValueError("成员对象无效")
                    # 平台缺省 uid 表示未识别成员；不能推断为自己或真人。
                    raw_uid = _field(row, "uid", "")
                    uid = _identifier(raw_uid) if raw_uid else ""
                    bot = _bot_flag(_field(row, "is_bot", _field(row, "isBot", False)))
                    members.append(PlatformMember(uid, bot, bool(own_uid and uid == own_uid)))
                rooms.append(RoomMembership(target, tuple(members)))
            return AreaMembership(area, "confirmed", tuple(rooms))
        except ValueError:
            return AreaMembership(area, "invalid_response")


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump(by_alias=False))
    return {key: _plain(item) for key, item in vars(value).items() if not key.startswith("_")}
