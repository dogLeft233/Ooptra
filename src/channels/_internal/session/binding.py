"""账号与浏览器绑定身份的唯一拥有者；共享传输复用必须越过旧会话屏障。"""

import asyncio
from uuid import uuid4

from channels._internal.adapters.voice import OopzDirectory, OopzRoomTransport
from channels._internal.output.duplex import VoiceDuplex
from channels._internal.session.service import RoomChannel
from channels.api import Duplex
from channels.models import LeaveRoom


class VoiceBinding:
    def __init__(self, bot, handler, controls, on_gap=None):
        self._bot = bot
        self.identity = uuid4().hex
        self._handler = handler
        self._on_gap = on_gap
        self._controls = controls
        self._channel = None
        self._directory = None
        self._duplex: Duplex | None = None
        self._retired = []
        self._closing = set()
        self._cleanup_failed = False

    @property
    def cleanup_pending(self):
        return bool(self._retired or self._closing or self._cleanup_failed)

    @property
    def connected(self):
        return self._bot is not None

    def _transport(self):
        return getattr(getattr(self._bot, "voice", None), "backend", None)

    @property
    def output(self) -> Duplex | None:
        return self._duplex

    @output.setter
    def output(self, duplex: Duplex | None) -> None:
        self._duplex = duplex
        if duplex is not None:
            self._controls().attach(duplex)

    def attach(self, duplex: Duplex) -> None:
        self.output = duplex
        duplex.set_remote_pcm_handler(self._handler)
        gap = getattr(duplex, "set_input_gap_handler", None)
        if gap is not None:
            gap(self._on_gap)

    @property
    def channel(self):
        if self._channel is None:
            self._channel = RoomChannel(OopzRoomTransport(self._bot, lambda:self._duplex, self._controls))
        return self._channel

    @property
    def directory(self):
        if self._directory is None:
            self._directory = OopzDirectory(self._bot)
        return self._directory

    def bind(self, bot):
        if bot is self._bot:
            return
        old_channel = self._channel
        previous_transport = self._transport()
        next_transport = getattr(getattr(bot, "voice", None), "backend", None)
        if self._channel is not None and previous_transport is next_transport:
            self._retired.append(self._channel)
        self._bot = bot
        self.identity = uuid4().hex
        self._channel = self._directory = None
        duplex = self._duplex
        if duplex is not None and (bot is None or (next_transport is not None and previous_transport is not next_transport)):
            self._duplex = None
            self._controls().reset_mute()
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                raise RuntimeError("渠道换绑必须在所属事件循环内执行") from None
            async def retire():
                try:
                    session = old_channel.snapshot().session if old_channel is not None else None
                    if old_channel is not None and session is not None:
                        result = await old_channel.execute(LeaveRoom(uuid4().hex, session, "system"))
                        if result.status != "completed":
                            raise RuntimeError("旧账号房间收尾未确认")
                finally:
                    await duplex.close()
            task = loop.create_task(retire(), name="channel-binding-retire")
            self._closing.add(task)
            def observed(done):
                self._closing.discard(done)
                if done.cancelled() or done.exception() is not None:
                    self._cleanup_failed = True
            task.add_done_callback(observed)

    async def drain_retired(self):
        while self._retired:
            channel = self._retired[0]
            session = channel.snapshot().session
            if session is not None:
                result = await channel.execute(LeaveRoom(uuid4().hex, session, "system"))
                if result.status != "completed":
                    return "旧传输尚未完成收尾，不能复用"
            self._retired.pop(0)
        return None

    async def prepare_output(self) -> Duplex:
        if self._cleanup_failed:
            raise RuntimeError("旧绑定释放失败，需恢复后重新启动")
        if self._closing:
            await asyncio.gather(*self._closing)
        error = await self.drain_retired()
        if error:
            raise RuntimeError(error)
        if self._bot is None:
            raise RuntimeError("Oopz账号尚未绑定")
        transport = self._transport()
        if self._duplex is None:
            if transport is None:
                raise RuntimeError("语音传输不可用")
            duplex = VoiceDuplex(transport)
            self._duplex = duplex
            self.attach(duplex)
            await duplex.start()
        assert self._duplex is not None
        await self._duplex.enable_listen(True)
        return self._duplex

    def resolve_speaker(self, uid):
        uid = str(uid or "unknown")
        if uid.isdecimal():
            states = getattr(getattr(self._bot, "voice", None), "voice_states", lambda:{})()
            for oopz_uid, state in states.items():
                if str(state.get("cid")) == uid:
                    return str(oopz_uid)
        return uid

    def subscribe_presence(self, handler):
        bot = self._bot
        registry = getattr(bot, "registry", None)
        if registry is None:
            return
        async def presence(_ctx, event):
            if bot is self._bot:
                handler(str(event.area or ""), str(event.channel or ""))
                handler(str(event.from_area or ""), str(event.from_channel or ""))
        registry.on("voice.enter", presence)
        registry.on("voice.leave", presence)

    async def close(self):
        session = self._channel.snapshot().session if self._channel is not None else None
        if self._channel is not None and session is not None:
            result = await self._channel.execute(LeaveRoom(uuid4().hex, session, "system"))
            if result.status != "completed":
                raise RuntimeError("渠道房间停止未确认")
        if self._cleanup_failed:
            raise RuntimeError("旧绑定释放失败")
        error = await self.drain_retired()
        if error:
            raise RuntimeError(error)
        if self._closing:
            await asyncio.gather(*self._closing)
        if self._duplex is not None:
            await self._duplex.close()
