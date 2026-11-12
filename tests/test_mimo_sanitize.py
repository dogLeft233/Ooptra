"""MiMo 级联的三个现场问题：说 think / 话太多 / 底噪当人话。

全部来自 2026-10-01 的实测，不是推测。取证办法是**把 TTS 产物再喂回 ASR** ——
你没法用耳朵听测试，但可以让 ASR 告诉你喇叭里到底念了什么：:

    TTS('🧘')   -> ASR 回读 "Somtime, think."      ← 用户报的「bot 会说 think 的内容」
    TTS('😤')   -> ASR 回读 4.48 秒英文乱语
    TTS('😐')   -> ASR 回读 "Yet Ismay's fine."
    TTS(' thinking用户说要短一点<｜end▁of▁thinking｜>好的') \
                -> ASR 回读 "Thinking 用户说要短一点。End of thinking 大鱼，好的"
    TTS('<no speechdetected>你这说半截卡壳了') \
                -> ASR 回读 "No speech detected，你这说半截卡壳了。"

结论：MiMo TTS 遇到 emoji 和控制标记**不跳过，而是照着编一段英文念出来**。
LLM 又极爱吐 emoji（本机 `data/voice_memory.jsonl` 里 9% 的回复带 emoji 或星号
动作），所以唯一的解法是在送进 TTS 之前清干净。
"""

from __future__ import annotations

import asyncio
import logging
import struct

import pytest
from support.channel_fixtures import establish_room
from support.dialogue_fixtures import mimo_backend as MimoCascadeBackend

from dialogue import (
    clamp_reply,
    looks_like_reasoning_leak,
    sanitize_asr,
    sanitize_for_tts,
)
from dialogue.models import VoiceReply
from operations.models import VoiceAgentSettings
from voice_agent.agent import _MIN_UTTERANCE_RMS, VoiceAgent

# ----------------------------------------------------------------------
# 1：emoji 必须清掉 —— 它是「说 think」的直接来源
# ----------------------------------------------------------------------


def test_emoji_are_removed_entirely() -> None:
    """🧘 会被念成 "Somtime, think."，不能留任何一个。"""
    assert sanitize_for_tts("🧘") == ""
    assert sanitize_for_tts("🏅 15连嗯！破纪录了！😏") == "15连嗯！破纪录了！"


def test_emoji_removed_from_a_real_reply() -> None:
    """真实回复（取自记忆里第 489 行附近的那条）。"""
    raw = "*扭头*  你！就！是！故！意！的！😤\n\nE宝已经词穷了，赢麻了 🏆"
    assert sanitize_for_tts(raw) == "扭头 你！就！是！故！意！的！E宝已经词穷了，赢麻了"


def test_common_symbols_and_flags_go_too() -> None:
    """🕯️🫡📡❌🏳️ 这些都在实际回复里出现过，同属一类。"""
    assert sanitize_for_tts("🕯️🫡📡❌🏳️") == ""


# ----------------------------------------------------------------------
# 2：控制标记必须清掉
# ----------------------------------------------------------------------


def test_think_tag_is_removed_including_the_unterminated_opener() -> None:
    """``' thinkingA<｜end▁of▁thinking｜>B'`` 里第一个 ``<`` 后面紧跟着另一个
    ``<``，通用「< 到 >」规则匹配不上，会把 ``thinking`` 当人话留在文本里念出去 ——
    这正是最初漏掉的那个 case。"""
    lt = "\x3c"  # 直接写字面量 < 在源码里容易被吞，这里显式转义
    raw = lt + "thinking用户说要短一点" + lt + "｜end▁of▁thinking｜>好的，没问题。"
    got = sanitize_for_tts(raw)
    assert "think" not in got.lower(), f"think 残留：{got!r}"
    assert got == "用户说要短一点 好的，没问题。"


def test_think_tag_variants() -> None:
    lt = "\x3c"
    for raw in (
        "＜think＞秘密＜/think＞答案",           # 全角尖括号
        lt + "thinking草稿" + lt + "/thinking" + ">" + "答案",   # 无闭合 >（被 max_tokens 截断）
        lt + "think" + ">草稿" + lt + "｜end▁of▁thinking｜>答案",
        "a" + lt + "|end▁of▁thinking|" + ">b",
    ):
        got = sanitize_for_tts(raw)
        assert "think" not in got.lower(), f"{raw!r} -> {got!r}"


def test_asr_marker_is_stripped_from_the_reply() -> None:
    assert sanitize_for_tts("<no speechdetected>你这说半截卡壳了") == "你这说半截卡壳了"


# ----------------------------------------------------------------------
# 3：Markdown / 分点 / 拟声词
# ----------------------------------------------------------------------


def test_markdown_and_bullets_are_stripped() -> None:
    raw = "**先说难在哪：**\n\n1. **弹反节奏是真的卡手。**\n2. 得看懂动作"
    got = sanitize_for_tts(raw)
    assert got == "先说难在哪：弹反节奏是真的卡手。得看懂动作"
    assert "1." not in got and "2." not in got
    assert "**" not in got
    assert "\n" not in got, "换行会被念成突兀的停顿，不能留"


def test_newlines_become_a_comma_stop() -> None:
    """规则里明写「不要换行」，模型照换不误 —— 换行在 TTS 里就是个停顿。"""
    assert sanitize_for_tts("第一行\n第二行\n第三行") == "第一行，第二行，第三行"
    assert sanitize_for_tts("好的\n\n\n没问题") == "好的，没问题"


def test_newline_after_punctuation_is_joined_not_commaed() -> None:
    """前一个字已经是标点时就并上去，否则会念出「。，」「：，」这种怪组合。

    「E宝：」这条是真从线上日志里捞出来的：字符类首版漏了「：」，结果把
    ``E宝：\\n你这是在…`` 清成了 ``E宝：，你这是在…``。
    """
    assert sanitize_for_tts("句号结尾。\n下一句") == "句号结尾。下一句"
    assert sanitize_for_tts("逗号结尾，\n下一句") == "逗号结尾，下一句"
    assert sanitize_for_tts("E宝：\n你这是在跟E包玩复读机吗？") == "E宝：你这是在跟E包玩复读机吗？"


def test_a_real_logged_reply_loses_its_newlines() -> None:
    """线上原样捞的一条（含「。」后换行、「：」后换行两种情形）。"""
    raw = "绝了，从嗯嗯系换到你好系？连续两条你好。\nE宝：\n你这是在跟E包玩复读机挑战吗？何意味……"
    got = sanitize_for_tts(raw)
    assert "\n" not in got
    assert "。，" not in got and "：，" not in got
    assert got.endswith("何意味……")


def test_only_whitespace_or_newlines_sanitizes_to_empty() -> None:
    """清完只剩我自己补的逗号 = 本来就没内容，得回空串，别让 TTS 念个停顿。"""
    assert sanitize_for_tts("\n") == ""
    assert sanitize_for_tts("\n\n") == ""
    assert sanitize_for_tts("  \n  ") == ""


def test_english_tics_become_chinese() -> None:
    """`emm` 会被 TTS 念成「哎母」，`Yeah` 念成英文。"""
    assert sanitize_for_tts("emm 你在说什么呀 Yeah") == "嗯 你在说什么呀 耶"
    assert sanitize_for_tts("emmm") == "嗯"
    assert sanitize_for_tts("Hmm") == "嗯"


def test_tics_do_not_eat_real_words() -> None:
    """拟声词替换是词边界匹配，不能把 EasonClaw / Open 这类词啃掉。"""
    assert "EasonClaw" in sanitize_for_tts("我是EasonClaw")
    assert "Open" in sanitize_for_tts("Open 一下")


def test_chinese_punctuation_survives() -> None:
    keep = "今天天气不错，出门记得带伞。真的吗？——我不信！"
    assert sanitize_for_tts(keep) == keep


def test_sanitize_is_idempotent() -> None:
    """``tts()`` 会再清一遍做兜底，重复调用必须稳定。"""
    raw = "🏅 emm *转身*  好的 😤\n\n再见"
    once = sanitize_for_tts(raw)
    assert sanitize_for_tts(once) == once


def test_leading_enumeration_is_not_mistaken_for_a_list_marker() -> None:
    """``77、78、79、80连嗯`` 是正文，不是分点 —— 不能每清一遍就少一段。

    回归锁：``_BULLET_RE`` 原来只写 ``^\\d+[.、)）]``，剥掉开头的 ``77、`` 之后
    ``78、`` 又落到行首，下一次再剥一层。于是 ``tts()`` 的兜底第二遍会改掉正文，
    存进记忆的和真正念出来的不再是同一句。
    """
    raw = "77、78、79、80连嗯……四连击？！"
    once = sanitize_for_tts(raw)
    assert once == raw, once
    assert sanitize_for_tts(once) == once, "清第二遍又变了"
    # 真的列表标记照剥不误
    assert sanitize_for_tts("1. 第一项") == "第一项"
    assert sanitize_for_tts("2、第二项") == "第二项"


# ----------------------------------------------------------------------
# 4：话太多 —— 长度闸门
# ----------------------------------------------------------------------


def test_clamp_keeps_short_text_untouched() -> None:
    assert clamp_reply("简短一句。") == "简短一句。"


def test_clamp_cuts_at_a_sentence_end() -> None:
    """窗口内最后一个句号在 limit//3 之后时，就在句末收，不切半句。"""
    got = clamp_reply("短句。" * 20, limit=40)
    assert got == "短句。" * 13
    assert got.endswith("。") and len(got) <= 40


def test_clamp_falls_back_to_ellipsis_when_no_sentence_end() -> None:
    got = clamp_reply("拖" * 200, limit=40)
    assert got.endswith("…") and len(got) == 41


def test_reasoning_leak_is_detected() -> None:
    """模型偶尔把内心独白写进正文，实测约 1%（391 条里 2 条）—— 得弃用整轮。"""
    assert looks_like_reasoning_leak(
        'The user is just saying "Hello" repeatedly. I should respond in a cute, tsundere way.'
    )
    assert looks_like_reasoning_leak(
        "The user said \"的人嘛\" which seems like a fragment, repeated twice. "
        "It doesn't make much sense."
    )


def test_normal_replies_are_not_mistaken_for_leaks() -> None:
    """别误杀：短英文句留着，中文夹英文名也留着。"""
    assert not looks_like_reasoning_leak("Oh,okay.")
    assert not looks_like_reasoning_leak("我叫EasonClaw，你可以叫我E包或者E宝。")
    assert not looks_like_reasoning_leak("还行，出门带把伞。")
    assert not looks_like_reasoning_leak("")  # 空串本来就不会被念
    # 中文里夹一个英文单词但整体仍是中文
    assert not looks_like_reasoning_leak("666，我的名字不是一宝，是E包/E宝，你自己听错了还怪我？")


def test_reasoning_leak_makes_chat_return_silence() -> None:
    """弃用整轮 = chat() 返回空串，agent 那边就不会推 TTS。"""
    leak = "The user is just saying Hello repeatedly. I should respond in a cute way."
    backend, _ = _chat_backend([], reply=leak)
    assert asyncio.run(backend.chat("你好", user_key="oopz:u1")) == ""


def test_a_normal_reply_still_comes_through() -> None:
    """别把闸门做成一刀切 —— 正常回复必须原样返回（对照组）。"""
    backend, _ = _chat_backend([], reply="还行，出门带把伞。")
    assert asyncio.run(backend.chat("天气如何", user_key="oopz:u1")) == "还行，出门带把伞。"


class _StubMemory:
    """只回历史，不落盘。"""

    def __init__(self, history: list[dict[str, str]]) -> None:
        self._history = history

    def as_messages(self, *, user_key: str = "", limit: int = 0) -> list[dict[str, str]]:
        return list(self._history)


class _FakeResp:
    status = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """只记下最后一次请求体，不发网络。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.body: dict | None = None

    def post(self, url, *, headers=None, json=None, proxy=None):
        self.body = json
        return _FakeResp(self._payload)


def _chat_backend(
    history: list[dict[str, str]], reply: str = "好。"
) -> tuple[MimoCascadeBackend, _FakeSession]:
    backend = MimoCascadeBackend(
        VoiceAgentSettings(enabled=True, barge_in=False, mimo_api_key="dummy-for-test"),
        _StubMemory(history),
    )
    session = _FakeSession({"choices": [{"message": {"content": reply}}]})

    async def _http():
        return session

    backend._http = _http  # type: ignore[method-assign]
    return backend, session


def _sent_assistant_history(backend: MimoCascadeBackend, session: _FakeSession) -> str:
    asyncio.run(backend.chat("在吗", user_key="oopz:u1"))
    assert session.body is not None, "没发出请求"
    turns = [m for m in session.body["messages"] if m["role"] == "assistant"]
    assert len(turns) == 1, f"历史条数不对：{turns}"
    return str(turns[0]["content"])


def test_assistant_history_is_clamped_before_it_reaches_the_model() -> None:
    """历史里存着净化前的长回复，模型会照着模仿 —— 只清不夹的话，prompt 里那句
    「不超过 35 个字」永远追不回来（记忆里最长的一条 150 字）。"""
    backend, session = _chat_backend([{"role": "assistant", "content": "拖" * 150}])
    sent = _sent_assistant_history(backend, session)
    assert len(sent) <= 46, f"历史没被夹：{len(sent)} 字"
    assert "拖" * 100 not in sent


def test_assistant_history_loses_its_newlines_too() -> None:
    backend, session = _chat_backend([{"role": "assistant", "content": "第一行\n第二行"}])
    sent = _sent_assistant_history(backend, session)
    assert "\n" not in sent, f"历史里的换行没清：{sent!r}"
    assert sent == "第一行，第二行"


# ----------------------------------------------------------------------
# 5：底噪当人话 —— 闸门
# ----------------------------------------------------------------------


def test_asr_marker_is_treated_as_no_speech() -> None:
    """ASR 对没人说话的片段不会返回空，会回一句标记；当成人话就会自问自答。"""
    assert sanitize_asr("<no speechdetected>") == ""
    assert sanitize_asr("No speech detected") == ""
    assert sanitize_asr("   ") == ""
    assert sanitize_asr("嗯。") == "嗯。"


def _pcm(amplitude: int, samples: int = 1600) -> bytes:
    return struct.pack("<h", amplitude) * samples


class _StubVad:
    """把 feed 的结果钉死，这样测的是闸门而不是 EnergyVad。"""

    def __init__(self, out: bytes | None) -> None:
        self._out = out

    def feed(self, pcm: bytes) -> bytes | None:
        return self._out

    def reset(self) -> None:
        pass


class _FakeBackend:
    name = "mimo_cascade"

    def __init__(self) -> None:
        self.called = 0

    async def handle_utterance(self, pcm, *, sample_rate, user_key="", channel_key="", on_recognition=None):
        self.called += 1
        return VoiceReply(user_text="嗯。", text="", pcm16=b"")


def _agent(utterance: bytes | None) -> tuple[VoiceAgent, _FakeBackend]:
    agent = VoiceAgent.__new__(VoiceAgent)
    agent.settings = VoiceAgentSettings(enabled=True, barge_in=False)
    agent.live_mode = False
    agent.duplex = None
    agent.backend = _FakeBackend()
    agent._busy = asyncio.Lock()
    agent._speaking = False
    agent._user_buffers = {}
    agent.settings.area = "area1"
    agent.settings.channel = "chan1"
    agent._init_throttle()  # 单槽信箱 + 冷却状态，见 agent.VoiceAgent._init_throttle
    agent._vad_for = lambda uid: _StubVad(utterance)  # type: ignore[method-assign]
    return agent, agent.backend


def test_noise_utterance_is_dropped_before_asr(caplog) -> None:
    """电平低于闸门的整段音频不送 ASR —— 「没人说话 bot 也接话」的根因。"""

    async def run() -> None:
        quiet = _pcm(int(0.0122 * 32768))  # 0.0122 > VAD 的 0.012，但 < 闸门 0.02
        assert _MIN_UTTERANCE_RMS > 0.012
        agent, backend = _agent(quiet)
        with caplog.at_level(logging.INFO, logger="voice_agent.agent"):
            await agent._on_remote_pcm("u1", quiet, 16000)
            await asyncio.sleep(0)  # 让被丢掉的路径不留下 task
        assert backend.called == 0, "底噪被送去 ASR 了"
        assert "丢弃疑似噪声回合" in caplog.text
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(agent.reply_scheduler.next(0), 0.01)

    asyncio.run(run())


def test_real_speech_still_goes_through(caplog) -> None:
    """真说话的音频必须原样进信箱，闸门不能误伤。

    注意长度：``_pcm`` 默认 1600 采样 = 0.1 秒，比 ``min_utterance_ms`` 还短，
    会被**时长**闸门（而不是能量闸门）拦下 —— 这里要的是真句子，给足 1.5 秒。
    """

    async def run() -> None:
        loud = _pcm(int(0.15 * 32768), samples=24000)  # 1.5s
        agent, backend = _agent(loud)
        await establish_room(agent)
        with caplog.at_level(logging.INFO, logger="voice_agent.agent"):
            await agent._on_remote_pcm("u1", loud, 16000)
            selected = await agent.reply_scheduler.next(0)
            await agent._run_selected_reply(selected)
        assert backend.called == 1
        assert "丢弃疑似噪声回合" not in caplog.text
        assert "丢弃过短回合" not in caplog.text

    asyncio.run(run())
