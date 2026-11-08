"""voice_agent 冒烟测试：不依赖网络与 Oopz。"""

from __future__ import annotations

import os
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from channels import create_vad  # noqa: E402
from channels.models import VadvConfig  # noqa: E402
from context import create_memory as MemoryStore  # noqa: E402
from dialogue import (  # noqa: E402  # noqa: E402
    pcm16_to_wav,
    resample_pcm16,
    wav_to_pcm16,
)
from operations import load_voice_agent_settings, resolve_agent_proxy_url  # noqa: E402


def test_settings_defaults() -> None:
    agent, api = load_voice_agent_settings()
    assert agent.backend in {"mimo_cascade", "gemini_live", "openai_realtime", ""} or agent.backend
    assert api.port == 3091 or api.port > 0
    assert hasattr(agent, "proxy")
    # 模型接入点与音色可配置
    assert agent.gemini_base_url.startswith("wss://")
    assert agent.gemini_voice
    assert agent.mimo_base_url.startswith("http")
    assert agent.mimo_tts_voice
    assert agent.openai_base_url.startswith("http")
    assert agent.openai_voice
    print("settings ok", agent.backend, api.port)


def test_resolve_agent_proxy_url() -> None:
    assert resolve_agent_proxy_url("") is None
    assert resolve_agent_proxy_url("direct") is None
    clash = resolve_agent_proxy_url("clash")
    assert clash is not None and clash.startswith("http://") and "7890" in clash
    explicit = resolve_agent_proxy_url("http://127.0.0.1:7890")
    assert explicit == "http://127.0.0.1:7890"
    print("proxy resolve ok", clash, explicit)


def test_memory() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "m.jsonl")
        store = MemoryStore(path, max_turns=5)
        store.set_persona("测试人格")
        store.append("user", "你好", user_key="qq:1")
        store.append("assistant", "你好呀", user_key="qq:1")
        msgs = store.as_messages(user_key="qq:1")
        assert msgs[-1]["content"] == "你好呀"
        assert store.get_persona() == "测试人格"
        assert store.clear(user_key="qq:1") == 2
    print("memory ok")


def test_memory_clear_all_is_not_a_noop() -> None:
    """WebUI 的「清空共享记忆」发的就是空 user_key —— 必须真的全清。

    回归锁：早期 ``clear()`` 的守卫是 ``if user_key and row.get("user_key") == user_key``，
    空 key 下这个条件恒假，一行都不删还返回 0，前端却照样弹「已清空」。
    """
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(os.path.join(td, "m.jsonl"), max_turns=5)
        store.append("user", "甲", user_key="oopz:1")
        store.append("assistant", "乙", user_key="oopz:1")
        store.append("user", "丙", user_key="oopz:2")
        assert store.clear() == 3, "传空 user_key 没有全清"
        assert store.recent(user_key="", limit=99) == []


def test_memory_clear_one_user_keeps_the_others() -> None:
    """带 user_key 时仍然只删那一个用户的，别把全清语义做过头。"""
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(os.path.join(td, "m.jsonl"), max_turns=5)
        store.append("user", "甲", user_key="oopz:1")
        store.append("user", "丙", user_key="oopz:2")
        assert store.clear(user_key="oopz:1") == 1
        left = store.recent(user_key="", limit=99)
        assert [r["content"] for r in left] == ["丙"]


def test_vad_silence_and_speech() -> None:
    vad = create_vad(VadvConfig(sample_rate=16000, silence_ms=120, energy_threshold=0.02))
    # 500ms 静音
    silence = struct.pack("<" + "h" * 800, *([0] * 800))
    assert vad.feed(silence) is None
    # 说话 200ms + 静音 200ms -> 一句
    loud = struct.pack("<" + "h" * 3200, *([8000] * 3200))
    tail = struct.pack("<" + "h" * 3200, *([0] * 3200))
    assert vad.feed(loud) is None
    utt = vad.feed(tail)
    assert utt is not None and len(utt) > 0
    print("vad ok", len(utt or b""))


def test_wav_roundtrip_and_resample() -> None:
    pcm = struct.pack("<" + "h" * 1600, *([1000] * 1600))
    wav = pcm16_to_wav(pcm, 16000)
    back, rate = wav_to_pcm16(wav)
    assert rate == 16000 and back == pcm
    up = resample_pcm16(pcm, 16000, 24000)
    assert len(up) > len(pcm)
    print("wav/resample ok", len(up))


if __name__ == "__main__":
    test_settings_defaults()
    test_resolve_agent_proxy_url()
    test_memory()
    test_vad_silence_and_speech()
    test_wav_roundtrip_and_resample()
    print("ALL SMOKE TESTS PASSED")
