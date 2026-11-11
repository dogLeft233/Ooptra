"""旧生成测试通过公开渠道接口建立会话，不再改写房间私有状态。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.voice import VoiceCoordinator
from channels.models import JoinRoom, RoomTarget
from operations.models import VoiceAgentSettings, VoiceApiSettings


async def establish_room(agent) -> None:
    if agent.channels.snapshot().confirmed:
        agent.behavior.start_session(agent.channels.snapshot().session)
        return

    async def join(**kwargs):
        return SimpleNamespace(rtc_channel_name="test-rtc")

    async def leave():
        pass

    if not agent.platform_bound:
        agent.bind_bot(SimpleNamespace(voice=SimpleNamespace(join=join, leave=leave, backend=None)))
    target = RoomTarget(agent.settings.area or "area1", agent.settings.channel or "chan1")
    result = await agent.channels.execute(
        JoinRoom(uuid4().hex, target, agent.channels.snapshot().revision)
    )
    assert result.status == "completed"
    agent.behavior.start_session(result.session)


class Duplex:
    transport = None
    output_muted = False
    output_volume = 50

    async def enable_listen(self, enabled=True):
        return {"ok": True}

    async def stop_tts(self):
        return {"ok": True}

    async def wait_tts_complete(self, timeout):
        return {"ok": True}

    async def close(self):
        return None


def make_voice_agent(tmp_path):
    class Voice:
        backend = None
        leaves = 0
        fail_leave = False

        async def join(self, **kwargs):
            await asyncio.sleep(0)
            return SimpleNamespace(rtc_channel_name="rtc")

        async def leave(self):
            if self.fail_leave:
                raise RuntimeError("leave failed")
            self.leaves += 1

    bot = SimpleNamespace(voice=Voice())
    agent = VoiceCoordinator(
        VoiceAgentSettings(
            enabled=True,
            backend="mimo_cascade",
            reply_probability_percent=100,
            voice_leave_enabled=False,
            voice_volume_enabled=False,
            memory_path=str(tmp_path / "memory.jsonl"),
        ),
        VoiceApiSettings(),
        bot=bot,
    )
    agent.test_platform = bot
    agent.duplex = Duplex()
    return agent
