"""onebot_v11 补充能力所需的最小网关实现。

原项目用一个 41KB 的 AsyncOopzGateway 承担全部业务能力；纯桥接只需要
onebot_v11 补充能力用到的那几个接口，这里直接用内置 SDK 实现。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from core.logger_config import get_logger

logger = get_logger("BridgeShim")


def to_legacy(value: Any) -> Any:
    """把 SDK 的 Pydantic 模型递归转成小驼峰字典。"""
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return {str(key): to_legacy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_legacy(item) for item in value]
    return value


class SdkBridgeShim:
    """替代原项目网关的最小实现，只暴露补充能力用到的接口。"""

    def __init__(
        self,
        bot: Any,
        *,
        default_area: str = "",
        default_channel: str = "",
    ) -> None:
        self.bot = bot
        self.default_area = str(default_area or "").strip()
        self.default_channel = str(default_channel or "").strip()

    def area_of(self, area: str | None) -> str:
        return str(area or self.default_area).strip()

    def channel_of(self, channel: str | None) -> str:
        return str(channel or self.default_channel).strip()

    async def get_person_infos_batch(
        self,
        uids: Iterable[str],
        **_kwargs: Any,
    ) -> dict[str, dict[str, Any]]:
        """名称解析器用它补全 UID -> 昵称。"""
        targets = [str(uid) for uid in uids if str(uid or "").strip()]
        output: dict[str, dict[str, Any]] = {}
        for index in range(0, len(targets), 30):
            chunk = targets[index : index + 30]
            try:
                people = await self.bot.person.get_person_infos_batch(chunk)
            except Exception as exc:
                logger.debug("批量获取用户信息失败: %s", exc)
                continue
            for person in people or []:
                data = to_legacy(person)
                uid = str(data.get("uid") or "")
                if uid:
                    output[uid] = data
        return output

    async def get_channel_messages(
        self,
        area: str | None = None,
        channel: str | None = None,
        size: int = 50,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        area = self.area_of(area)
        channel = self.channel_of(channel)
        if not area or not channel:
            return []
        try:
            messages = await self.bot.messages.get_channel_messages(area, channel, int(size))
        except Exception as exc:
            logger.debug("获取频道历史消息失败: %s", exc)
            return []
        return [to_legacy(message) for message in messages or []]

    async def edit_user_role(
        self,
        target_uid: str,
        role_id: int,
        add: bool,
        area: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        area = self.area_of(area)
        try:
            result = await self.bot.areas.edit_user_role(area, str(target_uid), int(role_id), bool(add))
        except Exception as exc:
            return {"error": str(exc)}
        ok = bool(getattr(result, "ok", True))
        message = str(getattr(result, "message", "") or "身份组已更新")
        return {"status": True, "message": message} if ok else {"error": message}


__all__ = ["SdkBridgeShim", "to_legacy"]
