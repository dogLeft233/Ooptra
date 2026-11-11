"""级联模式的回合节流：别每句都回，别让延迟越堆越高。

现场（2026-10-01 01:15~01:20，58 个真实回合）：**每一个都回了**。其中
``对。``(0.1s)、``哦。``(0.4s)、``哇。``(0.5s)、``我靠！``(0.5s) 这类一个字的气声
占相当一部分，bot 回一句「嗯嗯，知道了。」TTS 出声就是 2~5 秒。用户说下一句时
它还没说完 —— 队列只增不减，体感就是「延迟越来越高」。

根因在 ``agent.py``：每切出一句就 ``create_task``，任务在 ``_busy`` 锁**外面**
无上限排队。这里锁三件事：

1. **单槽信箱**：最多缓存一条，新的顶掉旧的（用户已经说下一句了，回上一句没意义）；
2. **冷却**：完整说完一条后隔 ``reply_cooldown_ms`` 才开下一轮。被抢话打断的
   回合**不计**冷却 —— 人家抢话要的就是立刻被回应；
3. **时长闸门**：短于 ``min_utterance_ms`` 的音频不送 ASR。与按能量过滤的
   ``_MIN_UTTERANCE_RMS`` 互补：一声音量够大但只有 0.1 秒的「对。」能量完全达标。
"""

from __future__ import annotations

import asyncio
import logging
import struct
import tempfile
from pathlib import Path

import pytest
from support.channel_fixtures import establish_room

from app.voice import VoiceCoordinator
from dialogue.models import DialogueCapabilities, Recognition, SettingsUpdate
from operations.models import VoiceAgentSettings, VoiceApiSettings

SR = 16000


def _pcm(amplitude: int, samples: int = 1600) -> bytes:
    return struct.pack("<h", amplitude) * samples


class _RecordingBackend:
    """记下每次真正跑到 ASR 的那段音频，好断言「回的是哪一条」。"""

    name = "mimo_cascade"

    def __init__(self) -> None:
        self.seen: list[bytes] = []

    async def aclose(self) -> None:
        pass

    def update_settings(self, settings, changed):
        self.settings = settings
        return SettingsUpdate()

    def capabilities(self):
        return DialogueCapabilities()

    async def recognize(self, audio, permit):
        self.seen.append(audio.pcm16)
        return Recognition(audio.input_ref, audio.session, permit.attempt_id, audio.user_key, "no_speech")


def _agent(utterance: bytes | None = None, **overrides) -> tuple[VoiceCoordinator, _RecordingBackend]:
    temporary = tempfile.TemporaryDirectory()
    agent = VoiceCoordinator(VoiceAgentSettings(enabled=True, memory_path=str(Path(temporary.name) / "history.jsonl")), VoiceApiSettings())
    agent.test_directory = temporary
    overrides.setdefault("reply_probability_percent", 100)
    overrides.setdefault("voice_leave_enabled", False)
    overrides.setdefault("voice_volume_enabled", False)
    agent.settings = VoiceAgentSettings(enabled=True, barge_in=False, memory_path=str(Path(temporary.name)/"history.jsonl"), **overrides)
    agent.live_mode = False
    agent.duplex = None
    agent.backend = _RecordingBackend()
    agent._busy = asyncio.Lock()
    agent._speaking = False
    agent.settings.area = "area1"
    agent.settings.channel = "chan1"
    agent.last_user_text = ""
    agent.last_reply = ""
    agent.turns = 0
    agent._init_throttle()
    if utterance:
        agent.settings.max_utterance_ms = max(30, int(len(utterance)/2/SR*1000))
    return agent, agent.backend


# ----------------------------------------------------------------------
# 时长闸门
# ----------------------------------------------------------------------


def test_short_utterance_is_dropped_before_asr(caplog) -> None:
    """0.3 秒的大声「我靠！」能量达标，但一个字的气声不该触发一轮。"""

    async def run() -> None:
        short = _pcm(int(0.15 * 32768), samples=int(SR * 0.3))
        agent, backend = _agent(short)
        with caplog.at_level(logging.INFO, logger="app.voice"):
            await agent._on_remote_pcm("u1", short, SR)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(agent.reply_scheduler.next(0), 0.01)
        assert backend.seen == []

    asyncio.run(run())


def test_a_real_sentence_clears_the_duration_gate(caplog) -> None:
    """对照：1.5 秒的真句子必须过闸（实测真说话 0.7s~15s 都有）。"""

    async def run() -> None:
        long_enough = _pcm(int(0.15 * 32768), samples=int(SR * 1.5))
        agent, _ = _agent(long_enough)
        with caplog.at_level(logging.INFO, logger="app.voice"):
            await agent._on_remote_pcm("u1", long_enough, SR)
        assert (await agent.reply_scheduler.next(0)).pcm == long_enough
        assert "丢弃过短回合" not in caplog.text

    asyncio.run(run())


def test_min_utterance_zero_disables_the_gate(caplog) -> None:
    """0 = 不过滤，一个字也照送（回滚用）。"""

    async def run() -> None:
        short = _pcm(int(0.15 * 32768), samples=int(SR * 0.12))
        agent, _ = _agent(short, min_utterance_ms=0)
        with caplog.at_level(logging.INFO, logger="app.voice"):
            await agent._on_remote_pcm("u1", short, SR)
        assert (await agent.reply_scheduler.next(0)).pcm == short
        assert "丢弃过短回合" not in caplog.text

    asyncio.run(run())


# ----------------------------------------------------------------------
# 生命周期
# ----------------------------------------------------------------------


def test_worker_starts_on_join_and_stops_on_leave() -> None:
    """退房必须收掉 worker 并清空信箱：上次没处理完的句子不能带到下次进房。"""

    async def run() -> None:
        agent, _ = _agent()
        await agent._start_round_worker()
        task = agent._round_task
        assert task is not None and not task.done()

        agent._offer_utterance("user", b"leftover", SR)
        await agent._stop_round_worker()

        assert agent._round_task is None
        assert task.done(), "worker 没被取消，退房后还在跑"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(agent.reply_scheduler.next(0), 0.01)

    asyncio.run(run())


def test_double_join_does_not_leave_two_workers() -> None:
    """重复进房时先收旧的 —— 两个 worker 会抢同一条，冷却也就形同虚设。"""

    async def run() -> None:
        agent, _ = _agent()
        await agent._start_round_worker()
        first = agent._round_task
        await agent._start_round_worker()

        assert first is not None and first.done(), "旧的 worker 还活着"
        assert agent._round_task is not first
        await agent._stop_round_worker()

    asyncio.run(run())


class _FakeLiveBackend:
    """够 ``_is_live_backend`` 认出来，且 aclose 可 await。"""

    name = "gemini_live"

    def update_settings(self, settings, changed):
        self.settings = settings
        return SettingsUpdate()

    def capabilities(self):
        return DialogueCapabilities(live=True)

    async def aclose(self) -> None:
        pass

    async def push_audio(self, pcm, sample_rate) -> None:
        pass


def test_hot_switch_to_cascade_starts_the_worker(monkeypatch) -> None:
    """live→cascade 必须拉起信箱消费端。

    回归锁：进房那一刻是 Live，``_start_round_worker`` 没跑；随后在 WebUI 里
    把后端改成 mimo_cascade，``refresh()`` 只换了 backend 没管 worker —— 句子
    进了信箱没人消费，bot 直接变哑巴，而且日志里什么都看不出来。
    """

    async def run() -> None:
        import app.voice as agent_mod

        agent, _ = _agent()
        agent.memory = object()
        await establish_room(agent)
        agent.live_mode = True  # 进房时是 Live，worker 没起来
        # 新后端是个「不像 Live」的（name=mimo_cascade、没有 push_audio）
        monkeypatch.setattr(agent_mod, "create_backend", lambda s, **kwargs: _RecordingBackend())

        await agent.refresh(["backend"])
        assert agent.live_mode is False, "前提没成立：没切成级联"
        assert agent._round_task is not None, "切到级联后信箱没人消费"
        await agent._stop_round_worker()

    asyncio.run(run())


def test_hot_switch_to_live_stops_the_worker(monkeypatch) -> None:
    """cascade→live 要收掉它，否则它抱着一条永远不会有新内容的信箱空转。"""

    async def run() -> None:
        import app.voice as agent_mod

        agent, _ = _agent()
        agent.memory = object()
        await establish_room(agent)
        agent.live_mode = True
        await agent._start_round_worker()
        task = agent._round_task
        assert task is not None

        # 切回 Live：create_backend 返回一个「像 Live」的后端
        monkeypatch.setattr(agent_mod, "create_backend", lambda s, **kwargs: _FakeLiveBackend())
        monkeypatch.setattr(agent_mod, "_is_live_backend", lambda b: True)
        await agent.refresh(["backend"])

        assert agent._round_task is None
        assert task.done(), "切回 Live 了，信箱消费端还在跑"

    asyncio.run(run())


def test_worker_consumes_an_offered_utterance() -> None:
    """端到端：喂进采集路径 → 信箱 → worker 真的把它送去了 ASR。"""

    async def run() -> None:
        spoken = _pcm(int(0.15 * 32768), samples=int(SR * 1.5))
        agent, backend = _agent(spoken, reply_cooldown_ms=0)
        await establish_room(agent)
        await agent._start_round_worker()
        await agent._on_remote_pcm("u1", spoken, SR)
        for _ in range(50):  # 等 worker 跑完，别用固定 sleep 拖慢套件
            if backend.seen:
                break
            await asyncio.sleep(0.01)
        await agent._stop_round_worker()
        assert backend.seen == [spoken]

    asyncio.run(run())
