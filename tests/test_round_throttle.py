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
import time
from types import SimpleNamespace

import pytest
from support.channel_fixtures import establish_room

from voice_agent.agent import VoiceAgent
from voice_agent.backends.base import VoiceReply
from voice_agent.settings import VoiceAgentSettings

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

    async def handle_utterance(self, pcm, *, sample_rate, user_key="", channel_key=""):
        self.seen.append(pcm)
        return VoiceReply(user_text="", text="", pcm16=b"")


class _StubVad:
    """把 feed 的结果钉死，这样测的是节流而不是 EnergyVad。"""

    def __init__(self, out: bytes | None) -> None:
        self._out = out

    def feed(self, pcm: bytes) -> bytes | None:
        return self._out

    def reset(self) -> None:
        pass


def _agent(utterance: bytes | None = None, **overrides) -> tuple[VoiceAgent, _RecordingBackend]:
    agent = VoiceAgent.__new__(VoiceAgent)
    overrides.setdefault("reply_probability_percent", 100)
    overrides.setdefault("voice_leave_enabled", False)
    overrides.setdefault("voice_volume_enabled", False)
    agent.settings = VoiceAgentSettings(enabled=True, barge_in=False, **overrides)
    agent.live_mode = False
    agent.duplex = None
    agent.backend = _RecordingBackend()
    agent._busy = asyncio.Lock()
    agent._speaking = False
    agent._barge_in_armed = True
    agent._user_buffers = {}
    agent.settings.area = "area1"
    agent.settings.channel = "chan1"
    agent.last_user_text = ""
    agent.last_reply = ""
    agent.turns = 0
    agent._init_throttle()
    agent._vad_for = lambda uid: _StubVad(utterance)  # type: ignore[method-assign]
    return agent, agent.backend


def _offer(agent: VoiceAgent, pcm: bytes, *, age: float = 0.0) -> None:
    """放进信箱，可选伪造「入槽时刻」以模拟已经等了很久。"""
    agent._offer_utterance("u1", pcm, SR)
    assert agent._pending is not None
    if age:
        uid, data, rate, _ = agent._pending
        agent._pending = (uid, data, rate, time.monotonic() - age)


def _take(agent: VoiceAgent) -> tuple[str, bytes, int, float]:
    """照 ``_round_worker`` 的取法把信槽取空。"""
    item, agent._pending = agent._pending, None
    agent._pending_event.clear()
    assert item is not None
    return item


# ----------------------------------------------------------------------
# 单槽信箱：只留最新的一条
# ----------------------------------------------------------------------


def test_mailbox_keeps_only_the_newest(caplog) -> None:
    """第三句进来时，前两句都得走 —— 用户已经往前说了，回旧的只会驴唇不对马嘴。"""

    async def run() -> None:
        agent, _ = _agent()
        with caplog.at_level(logging.INFO, logger="voice_agent.agent"):
            _offer(agent, b"first")
            _offer(agent, b"second")
            _offer(agent, b"third")
        assert agent._pending is not None
        assert agent._pending[1] == b"third", "信箱里留的不是最新的那条"
        assert caplog.text.count("丢弃积压回合") == 2

    asyncio.run(run())


def test_newest_utterance_wins_during_the_cooldown(caplog) -> None:
    """冷却期里用户又说了 → 改回**最新**的那条，不是把两句都回。

    这条是用户明确选的语义：「缓存最新一条，冷却一结束就回」。
    """

    async def run() -> None:
        agent, backend = _agent(reply_cooldown_ms=200)
        await establish_room(agent)
        _offer(agent, b"first")
        await agent._throttled_round(_take(agent))
        assert backend.seen == [b"first"]

        # 冷却期内用户先说了 stale，又补了 fresh
        _offer(agent, b"stale")
        task = asyncio.create_task(agent._throttled_round(_take(agent)))
        await asyncio.sleep(0.05)  # 让它进到 sleep(remaining)
        _offer(agent, b"fresh")
        await task

        assert backend.seen == [b"first", b"fresh"], "回的是被顶掉的那条"

    asyncio.run(run())


# ----------------------------------------------------------------------
# 冷却
# ----------------------------------------------------------------------


def test_cooldown_delays_the_next_round(monkeypatch) -> None:
    """完整说完一条后，下一条要等满冷却才开。"""

    async def run() -> None:
        from voice_agent import agent as agent_module

        agent, backend = _agent(reply_cooldown_ms=150)
        await establish_room(agent)
        now = [100.0]
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds

        # Windows monotonic clocks can round a 150ms real sleep down to 140ms.
        # Verify the required deadline and requested wait with a controlled clock.
        monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(agent_module.asyncio, "sleep", sleep)
        _offer(agent, b"first")
        await agent._throttled_round(_take(agent))
        assert agent._cooldown_until == pytest.approx(100.15)

        _offer(agent, b"second")
        await agent._throttled_round(_take(agent))

        assert waits == pytest.approx([0.15])
        assert agent._cooldown_until == pytest.approx(100.30)
        assert backend.seen == [b"first", b"second"]

    asyncio.run(run())


def test_cooldown_zero_means_no_wait() -> None:
    """0 = 关掉冷却，退回旧行为（一键回滚用）。"""

    async def run() -> None:
        agent, backend = _agent(reply_cooldown_ms=0)
        await establish_room(agent)
        started = time.monotonic()
        for payload in (b"a", b"b", b"c"):
            _offer(agent, payload)
            await agent._throttled_round(_take(agent))
        assert backend.seen == [b"a", b"b", b"c"]
        assert time.monotonic() - started < 0.5

    asyncio.run(run())


def test_barge_in_skips_the_cooldown() -> None:
    """被抢话打断的回合不设冷却 —— 抢话的人要的是立刻被回应，不是再等一轮。"""

    async def run() -> None:
        agent, _ = _agent(reply_cooldown_ms=5000)

        async def _barged(uid, pcm, sample_rate):  # 模拟 _on_remote_pcm 里的抢话
            await agent._interrupt_speaking()

        agent._handle_utterance = _barged  # type: ignore[method-assign]
        _offer(agent, b"x")
        await agent._throttled_round(_take(agent))

        assert agent._cooldown_until == 0.0, "被抢话了还设冷却，抢话的人要白等 5 秒"

    asyncio.run(run())


def test_normal_reply_does_set_the_cooldown() -> None:
    """对照组：没被抢话就得设冷却。别把上一条的语义做成一刀切。"""

    async def run() -> None:
        agent, _ = _agent(reply_cooldown_ms=5000)
        _offer(agent, b"x")
        await agent._throttled_round(_take(agent))
        assert agent._cooldown_until > time.monotonic()

    asyncio.run(run())


# ----------------------------------------------------------------------
# 过期
# ----------------------------------------------------------------------


def test_stale_round_is_dropped(caplog) -> None:
    """等到超过「冷却 + 3s」还没轮到 = 用户早说别的去了，回它等于自言自语。"""

    async def run() -> None:
        agent, backend = _agent(reply_cooldown_ms=0)
        await establish_room(agent)
        with caplog.at_level(logging.INFO, logger="voice_agent.agent"):
            _offer(agent, b"old", age=99.0)
            await agent._throttled_round(_take(agent))
        assert backend.seen == [], "过期回合还是送去 ASR 了"
        assert "丢弃过期回合" in caplog.text

    asyncio.run(run())


# ----------------------------------------------------------------------
# 时长闸门
# ----------------------------------------------------------------------


def test_short_utterance_is_dropped_before_asr(caplog) -> None:
    """0.3 秒的大声「我靠！」能量达标，但一个字的气声不该触发一轮。"""

    async def run() -> None:
        short = _pcm(int(0.15 * 32768), samples=int(SR * 0.3))
        agent, backend = _agent(short)
        with caplog.at_level(logging.INFO, logger="voice_agent.agent"):
            await agent._on_remote_pcm("u1", short, SR)
        assert agent._pending is None, "过短回合还是进了信箱"
        assert "丢弃过短回合" in caplog.text
        assert backend.seen == []

    asyncio.run(run())


def test_a_real_sentence_clears_the_duration_gate(caplog) -> None:
    """对照：1.5 秒的真句子必须过闸（实测真说话 0.7s~15s 都有）。"""

    async def run() -> None:
        long_enough = _pcm(int(0.15 * 32768), samples=int(SR * 1.5))
        agent, _ = _agent(long_enough)
        with caplog.at_level(logging.INFO, logger="voice_agent.agent"):
            await agent._on_remote_pcm("u1", long_enough, SR)
        assert agent._pending is not None, "真句子被时长闸门误伤了"
        assert "丢弃过短回合" not in caplog.text

    asyncio.run(run())


def test_min_utterance_zero_disables_the_gate(caplog) -> None:
    """0 = 不过滤，一个字也照送（回滚用）。"""

    async def run() -> None:
        short = _pcm(int(0.15 * 32768), samples=int(SR * 0.1))
        agent, _ = _agent(short, min_utterance_ms=0)
        with caplog.at_level(logging.INFO, logger="voice_agent.agent"):
            await agent._on_remote_pcm("u1", short, SR)
        assert agent._pending is not None
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

        _offer(agent, b"leftover")
        await agent._stop_round_worker()

        assert agent._round_task is None
        assert task.done(), "worker 没被取消，退房后还在跑"
        assert agent._pending is None, "残留的句子被带到下次进房了"

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
        import voice_agent.agent as agent_mod

        agent, _ = _agent()
        agent.memory = object()
        agent._make_vad = lambda: object()  # type: ignore[method-assign]
        await establish_room(agent)
        agent.live_mode = True  # 进房时是 Live，worker 没起来
        # 新后端是个「不像 Live」的（name=mimo_cascade、没有 push_audio）
        monkeypatch.setattr(agent_mod, "create_backend", lambda s, m: _RecordingBackend())

        await agent.refresh(["backend"])
        assert agent.live_mode is False, "前提没成立：没切成级联"
        assert agent._round_task is not None, "切到级联后信箱没人消费"
        await agent._stop_round_worker()

    asyncio.run(run())


def test_hot_switch_to_live_stops_the_worker(monkeypatch) -> None:
    """cascade→live 要收掉它，否则它抱着一条永远不会有新内容的信箱空转。"""

    async def run() -> None:
        import voice_agent.agent as agent_mod

        agent, _ = _agent()
        agent.memory = object()
        agent._make_vad = lambda: object()  # type: ignore[method-assign]
        await establish_room(agent)
        agent.live_mode = True
        await agent._start_round_worker()
        task = agent._round_task
        assert task is not None

        # 切回 Live：create_backend 返回一个「像 Live」的后端
        monkeypatch.setattr(agent_mod, "create_backend", lambda s, m: _FakeLiveBackend())
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
