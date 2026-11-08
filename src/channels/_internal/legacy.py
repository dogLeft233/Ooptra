"""过渡期 Oopz/VoiceDuplex 协议适配；不拥有房间或模型状态。"""

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from channels.api import AudioOutput, TransportFailure
from channels.models import AudioPushResult, RoomTarget

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
