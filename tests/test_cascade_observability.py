"""级联（cascade）模式的可观测性：识别了什么、推出去了没有，都要留痕。

这个文件存在的理由有两条，都是 2026-10-01 从 Gemini Live 切到 MiMo 级联时
现场发现的：

**1. cascade 整条链路是黑盒。** ``mimo_cascade.py`` 全文 248 行只有一条
``logger.debug("bad tts chunk")``。ASR 转写结果、LLM 回合、TTS 推流 —— 一条
都不记。切过去之后日志里只有 ``voice agent joined ... mode=cascade`` 一行，
之后全是空的；出问题只能靠 WebUI 的 ``last_user_text`` / ``last_reply`` 两个
字段猜，而那两个字段只有**最后一条**，既看不到历史，也看不出 VAD 有没有把句子
切碎（识别结果正常但音频只有 0.3 秒 = VAD 在切句，不是模型听不懂）。

**2. cascade 的推流循环直接丢弃 ``push_tts_pcm`` 的返回值。** Live 那条路在
B5（``test_voice_duplex_rebind.py``）里已经修过：页面已不是当前会话时
``push_tts_pcm`` 返回 ``{"ok": False, "error": "not joined"}``，被静默丢掉之后
就是「进房成功、日志一切正常、房间里一句话都没有」，且只有重启进程能恢复。
cascade 这一段当时漏了 —— 同一个坑，另一条路。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from support.channel_fixtures import establish_room

from voice_agent.agent import VoiceAgent
from voice_agent.backends.base import VoiceReply
from voice_agent.settings import VoiceAgentSettings

PCM = b"\x00\x01" * 16000  # 32000 字节 = 16000 样本 = 1.0s @16k


class FakeTransport:
    """假 duplex：只记推了什么，不做 IO。"""

    def __init__(self) -> None:
        self.pushes: list[tuple[bytes, int, bool]] = []
        self.push_result: dict[str, Any] = {"ok": True}

    async def push_tts_pcm(self, pcm: bytes, rate: int, *, finish: bool = False) -> dict:
        self.pushes.append((pcm, rate, finish))
        return dict(self.push_result)

    async def stop_tts(self) -> dict:
        return {"ok": True}

    async def enable_listen(self, enabled: bool = True) -> dict:
        return {"ok": True}


class FakeCascade:
    """假 cascade 后端：把预置的 VoiceReply 原样交回去。"""

    name = "mimo_cascade"

    def __init__(self, reply: VoiceReply) -> None:
        self.reply = reply

    async def handle_utterance(
        self, pcm: bytes, *, sample_rate: int, user_key: str = "", channel_key: str = ""
    ) -> VoiceReply:
        return self.reply


async def _make_agent(reply: VoiceReply) -> tuple[VoiceAgent, FakeTransport]:
    agent = VoiceAgent.__new__(VoiceAgent)
    agent._cascade_attempts = 0
    agent.settings = VoiceAgentSettings(enabled=True, sample_rate_out=24000)
    agent.duplex = FakeTransport()
    agent.backend = FakeCascade(reply)
    agent.live_mode = False
    agent._busy = asyncio.Lock()
    agent._speaking = False
    agent.turns = 0
    agent.last_reply = ""
    agent.last_user_text = ""
    agent.settings.area = "area1"
    agent.settings.channel = "chan1"
    await establish_room(agent)
    return agent, agent.duplex


def _reply(**kw: Any) -> VoiceReply:
    base = {
        "user_text": "今天天气怎么样",
        "text": "还行，出门带把伞。",
        "pcm16": b"\x11\x22" * 2880,  # 3 个 1920 字节的分片
        "sample_rate": 24000,
    }
    base.update(kw)
    return VoiceReply(**base)


# ----------------------------------------------------------------------
# 1：识别结果必须留痕（连同音频时长）
# ----------------------------------------------------------------------


def test_asr_turn_is_logged_with_text_and_duration(caplog) -> None:
    """一次回合要留下「识别成什么 + 答了什么 + 这段音频多长」。

    时长是关键：只有它能把「模型听不懂」和「VAD 把句子切碎了」区分开。
    """

    async def run() -> None:
        agent, _ = await _make_agent(_reply())
        with caplog.at_level(logging.INFO, logger="voice_agent.agent"):
            await agent._handle_utterance("u1", PCM, 16000)

        assert "今天天气怎么样" in caplog.text, "识别结果没留痕"
        assert "出门带把伞" in caplog.text, "回复没留痕"
        assert "1.0s" in caplog.text, "没有记这段音频有多长，看不出 VAD 切句"

    asyncio.run(run())


def test_empty_asr_result_is_still_logged(caplog) -> None:
    """识别为空也要记 —— 「听到了但没转出字」正是要排查的情形。"""

    async def run() -> None:
        agent, _ = await _make_agent(_reply(user_text="", text="", pcm16=b""))
        with caplog.at_level(logging.INFO, logger="voice_agent.agent"):
            await agent._handle_utterance("u1", PCM, 16000)

        assert "ASR 回合" in caplog.text, "识别为空时什么都不记，等于没发生过"

    asyncio.run(run())


# ----------------------------------------------------------------------
# 2：推流失败必须留痕（B5 的同款坑，cascade 这条路上一直没补）
# ----------------------------------------------------------------------


def test_push_failure_is_logged_once_per_turn(caplog) -> None:
    """整回合只报一条 WARNING；失败通常整回合都失败，逐分片刷屏会盖住线索。"""

    async def run() -> None:
        agent, transport = await _make_agent(_reply())
        transport.push_result = {"ok": False, "error": "not joined"}

        with caplog.at_level(logging.WARNING, logger="voice_agent.agent"):
            await agent._handle_utterance("u1", PCM, 16000)

        warned = [r for r in caplog.records if "推流失败" in r.getMessage()]
        assert len(warned) == 1, f"同一回合报了 {len(warned)} 条，应该只有 1 条"
        assert "transport_failed" in warned[0].getMessage(), "没带上是哪种失败"

    asyncio.run(run())


def test_push_success_is_silent(caplog) -> None:
    """正常推流不能有任何 WARNING，否则这条日志会变成噪音。"""

    async def run() -> None:
        agent, transport = await _make_agent(_reply())
        with caplog.at_level(logging.WARNING, logger="voice_agent.agent"):
            await agent._handle_utterance("u1", PCM, 16000)

        assert transport.pushes, "根本没推流，用例是假通过"
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    asyncio.run(run())


def test_turn_counter_and_speaking_are_restored(caplog) -> None:
    """推完要把 ``_speaking`` 落回去、回合数 +1（cascade 的回合边界就在这里）。"""

    async def run() -> None:
        agent, _ = await _make_agent(_reply())
        await agent._handle_utterance("u1", PCM, 16000)

        assert agent.turns == 1, "cascade 的回合数没有 +1"
        assert agent._speaking is False, "推完没有落回 False"
        assert agent.last_user_text == "今天天气怎么样"
        assert agent.last_reply == "还行，出门带把伞。"

    asyncio.run(run())
