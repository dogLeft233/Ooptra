"""经公开 protocol=1 请求准备合成 AstrBot 来源。"""

import asyncio
from typing import Any


async def prepare_source(bridge: Any, result: dict, area: str = "a", channel: str = "c") -> None:
    await bridge.poll(wait=0)
    preparation = asyncio.create_task(bridge.prepare(area, channel))
    await asyncio.sleep(0)
    event = (await bridge.poll(wait=0))[0]
    assert bridge.resolve(event["request_id"], result)
    await preparation
