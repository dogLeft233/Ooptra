"""语音 duplex 必须跟着 bot 换绑，且推流失败不能静默。

这个文件存在的理由（B5）：``bridge/app.py`` 的 ``_ensure_running`` 每次重建都
``OopzBot(...)`` —— 每个 bot 自带新的 ``Voice`` 与新的 ``BrowserVoiceTransport``
（新的 Playwright 浏览器与页面）。但 ``VoiceAgent.bind_bot`` 只做了
``self._bot = bot``，而 ``join()`` 里是 ``if self.duplex is None:`` 才绑定，
于是 duplex **在进程启动后的第一次 join 之后就再也不会换**。

后果是双向同时失效，而日志一切正常：

  * 模型音频经旧 duplex 推进**已经关掉的页面**，那里 ``client`` 为空，
    ``agoraPushTtsPcm`` 返回 ``{"ok": False, "error": "not joined"}`` ——
    而这个返回值在 ``on_audio`` 里被直接丢弃，连一条日志都没有；
  * ``set_remote_pcm_callback`` 注册在旧 transport 上，新页面的
    ``oopzPushRemotePcm`` 永远不上报 → agent 一个字都听不到。

线上现象：**进房成功、``Live 回合`` 照常出现、房间里一句话都没有**，且只有
重启**进程**才能恢复（重启桥接只会再切一次）。2026-09-30 实测踩到。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from channels import create_duplex as VoiceDuplex
from operations import load_voice_agent_settings
from voice_agent.agent import VoiceAgent

# ----------------------------------------------------------------------
# 替身：真 VoiceDuplex + 假 transport / 假 bot（不碰 Playwright 与网络）
# ----------------------------------------------------------------------


class FakeTransport:
    """假 BrowserVoiceTransport：只记状态，不做 IO。"""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.started = 0
        self.closed = 0
        self.remote_cb: Any = None
        self.listen_calls: list[bool] = []
        self.pushes: list[tuple[bytes, int, bool]] = []
        self.push_result: dict[str, Any] = {"ok": True}

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1

    def set_remote_pcm_callback(self, callback: Any) -> None:
        self.remote_cb = callback

    async def enable_listen(self, enabled: bool = True) -> dict:
        self.listen_calls.append(enabled)
        return {"ok": True}

    async def push_tts_pcm(self, pcm: bytes, rate: int, *, finish: bool = False, volume: int = 50) -> dict:
        self.pushes.append((pcm, rate, finish))
        return dict(self.push_result)

    async def stop_tts(self) -> dict:
        return {"ok": True}


class FakeBackend:
    """假语音后端；``set_*_out`` 与真后端同款。"""

    def __init__(self) -> None:
        self.session_active = True
        self._audio_out: Any = None
        self._turn_out: Any = None
        self.session_starts = 0

    def set_audio_out(self, handler: Any) -> None:
        self._audio_out = handler

    def set_text_out(self, handler: Any) -> None:
        pass

    def set_turn_out(self, handler: Any) -> None:
        self._turn_out = handler

    async def start_session(self) -> None:
        self.session_starts += 1

    async def push_audio(self, pcm: bytes, rate: int | None = None) -> None:
        pass

    async def interrupt(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def emit_audio(self, n: int = 1) -> None:
        assert self._audio_out is not None, "_wire_live 没有注册音频回调"
        for _ in range(n):
            await self._audio_out(b"\x01\x02" * 480, 24000)

    async def end_turn(self, interrupted: bool = False) -> None:
        assert self._turn_out is not None, "_wire_live 没有注册 turn 回调"
        await self._turn_out(interrupted)


class FakeVoice:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    async def join(self, *, area: str = "", channel: str = "") -> Any:
        return type("Sign", (), {"rtc_channel_name": "rtc-1"})()

    async def leave(self) -> None:
        pass


class FakeBot:
    def __init__(self, transport: FakeTransport) -> None:
        self.voice = FakeVoice(transport)


def _make_agent() -> VoiceAgent:
    agent = VoiceAgent.__new__(VoiceAgent)
    agent.settings = load_voice_agent_settings()[0]
    # CI 的 config.py 由 config.example.py 生成（enabled=False）；语音关掉时
    # join/_on_remote_pcm 都会在第一道门返回，用例会假通过。
    agent.settings.enabled = True
    agent.settings.backend = "gemini_live"
    agent.settings.barge_in = True
    agent.settings.listen_only_uids = []
    agent.duplex = None
    agent._lifecycle_tasks = set()
    agent.backend = FakeBackend()
    agent.live_mode = True
    agent._bot = None
    agent._speaking = False
    agent._live_output_lock = asyncio.Lock()
    agent._user_buffers = {}
    agent._vad = None
    agent._task = None
    agent.last_reply = ""
    agent.last_user_text = ""
    agent.turns = 0
    agent.settings.area = "area1"
    agent.settings.channel = "chan1"
    agent.memory = type(
        "M", (), {"append": lambda *a, **k: None, "set_persona": lambda *a: None}
    )()
    return agent


async def _tidy(agent: VoiceAgent) -> None:
    """收掉 pump 任务，避免事件循环关闭时报 pending task 警告。"""
    duplex = agent.duplex
    if duplex is not None:
        await duplex.close()
    await _settle(agent)


async def _settle(agent: VoiceAgent) -> None:
    """等 bind_bot 交给事件循环的那次 close() 真正跑完（不等就是不确定的）。"""
    await asyncio.gather(*agent._lifecycle_tasks, return_exceptions=True)


# ----------------------------------------------------------------------
# 1~3：换 bot（=「重启桥接」）必须换绑 duplex
# ----------------------------------------------------------------------


def test_bind_bot_drops_duplex_bound_to_old_transport() -> None:
    """换 bot 后 duplex 必须作废 —— 不能继续指着旧浏览器。"""

    async def run() -> None:
        agent = _make_agent()
        old_transport = FakeTransport("old")
        agent.bind_bot(FakeBot(old_transport))
        await agent.join("area1", "chan1")

        assert agent.duplex is not None
        assert agent.duplex.transport is old_transport
        assert agent._joined is True

        # 「重启桥接」：新 bot 自带新的 transport
        new_transport = FakeTransport("new")
        agent.bind_bot(FakeBot(new_transport))

        assert agent.duplex is None, "换 bot 后旧 duplex 必须作废"
        assert agent._joined is False, "bot 都没了，不能还认为自己在房里"
        await _settle(agent)
        assert old_transport.closed == 1, "旧 transport 要关掉，不能留着漏"

    asyncio.run(run())


def test_bind_bot_keeps_duplex_for_same_transport() -> None:
    """同一个 transport 的重复绑定时不能误伤（否则每次重连都重建一套）。"""

    async def run() -> None:
        agent = _make_agent()
        transport = FakeTransport("same")
        bot = FakeBot(transport)
        agent.bind_bot(bot)
        await agent.join("area1", "chan1")
        bound = agent.duplex

        agent.bind_bot(bot)  # 同一个 bot 再绑一次

        assert agent.duplex is bound
        assert transport.closed == 0

        await _tidy(agent)

    asyncio.run(run())


def test_join_rebuilds_duplex_on_new_bot_end_to_end() -> None:
    """核心回归：换 bot 后再 join，语音必须走**新** transport（双向）。"""

    async def run() -> None:
        agent = _make_agent()
        old_transport = FakeTransport("old")
        agent.bind_bot(FakeBot(old_transport))
        await agent.join("area1", "chan1")

        new_transport = FakeTransport("new")
        agent.bind_bot(FakeBot(new_transport))
        await agent.join("area1", "chan1")

        assert agent.duplex is not None
        assert agent.duplex.transport is new_transport
        assert new_transport.started == 1, "新 transport 必须真的启用"
        # join() 会开两次监听：重建 duplex 后一次，voice.join 之后再一次
        assert new_transport.listen_calls == [True, True]

        # 上行：模型的音频必须落到新 transport 上（以前会推进死页面）
        await agent.backend.emit_audio(2)
        assert len(new_transport.pushes) == 2
        assert old_transport.pushes == [], "音频不能再进旧页面"

        # 下行：远端 PCM 回调必须注册在新 transport 上（以前听不到人说话）
        assert new_transport.remote_cb is not None
        new_transport.remote_cb("u1", b"\x00\x01" * 320, 16000)
        assert agent.backend.session_starts >= 1

        await _tidy(agent)

    asyncio.run(run())


def test_join_rebinds_when_duplex_is_stale_without_bind_bot() -> None:
    """兜底：即使 bind_bot 没被调用，join 也要按 transport 身份自愈。"""

    async def run() -> None:
        agent = _make_agent()
        stale = FakeTransport("stale")
        agent.duplex = VoiceDuplex(stale)
        agent.bind_duplex(agent.duplex)  # 装作进程早期绑过一次

        current = FakeTransport("current")
        agent._bot = FakeBot(current)  # bot 已被替换，duplex 还指着旧的
        await agent.join("area1", "chan1")

        assert agent.duplex.transport is current
        assert stale.closed == 1, "join 里的兜底路径要就地关掉过期的 transport"

        await _tidy(agent)

    asyncio.run(run())


# ----------------------------------------------------------------------
# 4~5：推流失败必须留痕（这次排查绕远路的直接原因就是它被静默丢弃）
# ----------------------------------------------------------------------


def test_push_failure_is_logged_once_per_turn(caplog) -> None:
    """整回合推流失败只报一条 WARNING；下一回合若还坏，再报一条。"""

    async def run() -> None:
        agent = _make_agent()
        transport = FakeTransport("t")
        transport.push_result = {"ok": False, "error": "not joined"}
        agent.bind_bot(FakeBot(transport))
        await agent.join("area1", "chan1")

        with caplog.at_level(logging.WARNING, logger="voice_agent.agent"):
            await agent.backend.emit_audio(1)
            await agent.backend.emit_audio(1)  # 同一回合的第 2 个分片
            first_turn = [r for r in caplog.records if "推流失败" in r.getMessage()]
            assert len(first_turn) == 1, "同一回合不能逐分片刷屏"
            assert "transport_failed" in first_turn[0].getMessage()

            await agent.backend.end_turn()
            await agent.backend.emit_audio(1)
            total = [r for r in caplog.records if "推流失败" in r.getMessage()]
            assert len(total) == 2, "新回合仍失败要再报一条，否则问题会被跳过"

        await _tidy(agent)

    asyncio.run(run())


def test_push_success_is_silent(caplog) -> None:
    """正常推流不能有任何 WARNING（否则这条日志会变成噪音）。"""

    async def run() -> None:
        agent = _make_agent()
        transport = FakeTransport("t")
        agent.bind_bot(FakeBot(transport))
        await agent.join("area1", "chan1")

        with caplog.at_level(logging.WARNING, logger="voice_agent.agent"):
            await agent.backend.emit_audio(3)
            await agent.backend.end_turn()
            assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

        await _tidy(agent)

    asyncio.run(run())


def test_push_failure_then_recovery_logs_again_on_next_failure(caplog) -> None:
    """失败 → 恢复 → 再失败，要重新报（不能因为报过一次就永久闭嘴）。"""

    async def run() -> None:
        agent = _make_agent()
        transport = FakeTransport("t")
        transport.push_result = {"ok": False, "error": "not joined"}
        agent.bind_bot(FakeBot(transport))
        await agent.join("area1", "chan1")

        with caplog.at_level(logging.WARNING, logger="voice_agent.agent"):
            await agent.backend.emit_audio(1)  # 失败
            transport.push_result = {"ok": True}
            await agent.backend.emit_audio(1)  # 恢复
            transport.push_result = {"ok": False, "error": "not joined"}
            await agent.backend.emit_audio(1)  # 再失败
            assert len([r for r in caplog.records if "推流失败" in r.getMessage()]) == 2

        await _tidy(agent)

    asyncio.run(run())
