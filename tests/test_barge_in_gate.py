"""Live 抢话门限：要「连续说够久」才算抢话，一帧就打断的时代结束了。

这个文件存在的理由（B6）：服务端的自动抢话已经关掉
（``gemini_live._realtime_input_config`` 里的 ``NO_INTERRUPTION``），因为灌进去的是
**整个语音房间的混流**，服务端只要认出「有人在说话」就取消生成 —— 2026-09-30 实测
投喂真语音，从 0.02（远处背景人声）到 0.15（贴麦）**任何音量都会打断 bot**，热闹的
房间里 bot 一句完整的话都说不完。

于是抢话改由客户端判，判据从「有人出声」改成「某人**连续**说话够久」：

  * 一声咳嗽、一次键盘、一个「嗯」—— 攒不到 ``barge_in_hold_ms`` 就清零，bot 继续说
  * 真的有人对着它说满门限 —— 让路

注意「连续」是关键字：说话一断就清零，而不是累计总时长。否则房间里断断续续的
背景聊天会慢慢攒够，门限形同虚设。

``barge_in_hold_ms = 0`` 是**有意保留的退路**：一键回到旧行为，出问题时能对照。
"""

from __future__ import annotations

import asyncio
from typing import Any

from support.channel_fixtures import establish_room

from operations import load_voice_agent_settings
from voice_agent.agent import VoiceAgent

#: 16k / mono / 16bit 的 20ms 音频帧。
#: 采样值 0x4000 = 16384 → 归一化 RMS 0.5，稳过 _BARGE_IN_ENERGY(0.012)
LOUD_FRAME = b"\x00\x40" * 320
#: 同一个长度但几乎无声（RMS≈0.0078），低于能量阈值
QUIET_FRAME = b"\x00\x01" * 320
FRAME_MS = 20
HOLD_MS = 300
#: 攒够 HOLD_MS 需要的帧数（20ms 一帧）
FRAMES_TO_HOLD = HOLD_MS // FRAME_MS  # 15


class FakeDuplex:
    output_muted = False
    output_volume = 50

    def __init__(self) -> None:
        self.stop_calls = 0

    async def start(self) -> None:
        pass

    async def enable_listen(self, on: bool = True) -> dict:
        return {"ok": True}

    async def push_tts_pcm(self, pcm: bytes, rate: int, *, finish: bool = False) -> dict:
        return {"ok": True}

    async def stop_tts(self) -> dict:
        self.stop_calls += 1
        return {"ok": True}

    async def close(self) -> None:
        pass


class FakeBackend:
    def __init__(self) -> None:
        self.interrupt_calls = 0
        self.pushed_frames = 0
        self.session_active = True
        self._audio_out: Any = None
        self._turn_out: Any = None

    def set_audio_out(self, handler: Any) -> None:
        self._audio_out = handler

    def set_text_out(self, handler: Any) -> None:
        pass

    def set_turn_out(self, handler: Any) -> None:
        self._turn_out = handler

    async def interrupt(self) -> None:
        self.interrupt_calls += 1

    async def push_audio(self, pcm: bytes, rate: int | None = None) -> None:
        self.pushed_frames += 1

    async def start_session(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def emit_audio(self, n: int = 1) -> None:
        """模型吐 n 个音频分片 —— 也就是把 _speaking 顶起来。"""
        for _ in range(n):
            assert self._audio_out is not None
            await self._audio_out(b"\x01\x02" * 480, 24000)

    async def end_turn(self, interrupted: bool = False) -> None:
        assert self._turn_out is not None
        await self._turn_out(interrupted)


async def _make_agent(*, hold_ms: int = HOLD_MS) -> tuple[VoiceAgent, FakeDuplex, FakeBackend]:
    agent = VoiceAgent.__new__(VoiceAgent)
    agent.settings = load_voice_agent_settings()[0]
    # CI 的 config.py 来自 config.example.py（enabled=False）；关掉时 _on_remote_pcm
    # 在第一道门就 return，所有「0 次打断」的断言都会假通过。
    agent.settings.enabled = True
    agent.settings.backend = "gemini_live"
    agent.settings.barge_in = True
    agent.settings.barge_in_hold_ms = hold_ms
    agent.settings.listen_only_uids = []
    agent.settings.sample_rate_in = 16000
    agent._bot = None
    agent._speaking = False
    agent._live_output_lock = asyncio.Lock()
    agent._user_buffers = {}
    agent._vad = None
    agent._task = None
    agent.duplex = None
    agent.memory = type(
        "M", (), {"append": lambda *a, **k: None, "set_persona": lambda *a: None}
    )()
    agent.last_reply = ""
    agent.last_user_text = ""
    agent.turns = 0
    agent.settings.area = "area1"
    agent.settings.channel = "chan1"

    duplex, backend = FakeDuplex(), FakeBackend()
    agent.duplex = duplex
    agent.backend = backend
    agent.live_mode = True
    await establish_room(agent)
    return agent, duplex, backend


async def _setup(hold_ms: int = HOLD_MS) -> tuple[VoiceAgent, FakeDuplex, FakeBackend]:
    agent, duplex, backend = await _make_agent(hold_ms=hold_ms)
    # 必须真的走一遍 _wire_live：不然 backend 的 audio_out/turn_out 没注册，
    # emit_audio / end_turn 无从驱动 _speaking，门限根本走不到。
    await agent._wire_live()
    return agent, duplex, backend


async def _say(agent: VoiceAgent, backend: FakeBackend, frames: int, frame: bytes = LOUD_FRAME) -> None:
    """模型先出声（否则 _speaking 为假，门限不会被走到），再灌 frames 帧。"""
    await backend.emit_audio(1)
    for _ in range(frames):
        await agent._on_remote_pcm("u1", frame, 16000)


def test_short_blip_does_not_barge_in() -> None:
    """一声咳嗽的长度（100ms）不该打断 bot —— 这是本次修复的核心。"""

    async def run() -> None:
        agent, duplex, backend = await _setup()
        await _say(agent, backend, FRAMES_TO_HOLD - 5)  # 200ms，差 100ms

        assert backend.pushed_frames == FRAMES_TO_HOLD - 5, "帧没进 Live 会话，本例不作数"
        assert backend.interrupt_calls == 0, "门限没拦住短促的声响"
        assert duplex.stop_calls == 0

    asyncio.run(run())


def test_sustained_speech_barges_in_once() -> None:
    """真有人连续说满门限 → 让路，且**只让一次**（不是每帧一次）。"""

    async def run() -> None:
        agent, duplex, backend = await _setup()
        await _say(agent, backend, FRAMES_TO_HOLD * 3)  # 900ms，远超门限

        assert backend.pushed_frames == FRAMES_TO_HOLD * 3, "帧没进 Live 会话，本例不作数"
        assert backend.interrupt_calls == 1, f"打断被触发 {backend.interrupt_calls} 次"
        assert duplex.stop_calls == 1

    asyncio.run(run())


def test_a_gap_resets_the_counter() -> None:
    """门限衡量的是**连续**时长：说 200ms、停一下、再说 200ms，不该打断。"""

    async def run() -> None:
        agent, duplex, backend = await _setup()
        half = FRAMES_TO_HOLD // 2
        await _say(agent, backend, half)
        await agent._on_remote_pcm("u1", QUIET_FRAME, 16000)  # 断了
        for _ in range(half):
            await agent._on_remote_pcm("u1", LOUD_FRAME, 16000)

        assert backend.interrupt_calls == 0, "断开的两次说话被当成了一次连续的"
        assert duplex.stop_calls == 0

    asyncio.run(run())


def test_quiet_frames_never_accumulate() -> None:
    """低电平的帧（背景底噪/键盘）不计入时长，攒多久都不打断。"""

    async def run() -> None:
        agent, duplex, backend = await _setup()
        await _say(agent, backend, FRAMES_TO_HOLD * 5, frame=QUIET_FRAME)

        assert backend.pushed_frames == FRAMES_TO_HOLD * 5, "帧没进 Live 会话，本例不作数"
        assert backend.interrupt_calls == 0, "背景底噪把 bot 打断了"
        assert duplex.stop_calls == 0

    asyncio.run(run())


def test_hold_ms_zero_restores_immediate_barge_in() -> None:
    """``barge_in_hold_ms = 0`` 是有意保留的退路：回到「第一帧就打断」。"""

    async def run() -> None:
        agent, duplex, backend = await _setup(hold_ms=0)
        await _say(agent, backend, 1)

        assert backend.interrupt_calls == 1, "门限关掉后没有退回旧行为"
        assert duplex.stop_calls == 1

    asyncio.run(run())


def test_rearm_clears_accumulated_speech() -> None:
    """上回合攒的时长不能带进下一回合 —— 否则 bot 一开口就立刻满足门限。"""

    async def run() -> None:
        agent, duplex, backend = await _setup()
        await _say(agent, backend, FRAMES_TO_HOLD - 1)  # 差一帧，没打断
        assert backend.interrupt_calls == 0

        await backend.end_turn(False)  # 模型说完了 → 重新武装

        # 新回合：只说了 100ms。若上回合残留的 280ms 没被清掉，这里会立刻打断
        await _say(agent, backend, 5)
        assert backend.interrupt_calls == 0, "上回合累计的时长泄漏到了下一回合"
        assert duplex.stop_calls == 0

    asyncio.run(run())


def test_gate_is_per_user() -> None:
    """每个人各算各的：两个人各说 200ms 不构成「某人连续说了 400ms」。"""

    async def run() -> None:
        agent, duplex, backend = await _setup()
        await backend.emit_audio(1)
        half = FRAMES_TO_HOLD // 2
        for _ in range(half):
            await agent._on_remote_pcm("u1", LOUD_FRAME, 16000)
            await agent._on_remote_pcm("u2", LOUD_FRAME, 16000)

        assert backend.interrupt_calls == 0, "把房间里几个人的说话时长加在一起算了"
        assert duplex.stop_calls == 0

    asyncio.run(run())
