"""过渡期 Oopz/VoiceDuplex 协议适配；不拥有房间或模型状态。"""

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


class LegacyRoomTransport:
    def __init__(self, bot: Any, output: Callable[[], AudioOutput | None]) -> None:
        self._bot = bot
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

    async def push_audio(self, pcm: bytes, rate: int, *, finish: bool = False) -> AudioPushResult:
        output = self._bound_output
        if output is None:
            return AudioPushResult(False, "output_unavailable")
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


class LegacyPlatformDirectory:
    """仅此处了解 SDK 返回的属性/字典别名和平台缺省值。"""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

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
