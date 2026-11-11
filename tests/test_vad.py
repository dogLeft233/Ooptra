"""EnergyVad 切出来的句子必须**真的装着那句话**。

2026-10-01 发现：``feed()`` 早期直接返回 ``bytes(self._buffer)``，而 ``_buffer``
是「还没凑够一帧的输入暂存区」，边读边 ``del``，说话过程中一直接近空。于是每一句
都只剩最后一帧 —— 约 30ms 的**静音尾巴**，真正的语音被逐帧丢掉。

现场表现（级联模式）：

* ASR 收到的永远是几十毫秒静音，对话式 ASR 不返回空、会照着**编**一句，
  日志里 turns 一路涨，实际全是幻觉；
* 用户听到的就是「识别不准」；
* 播放器推底噪时编出「嗯。」，表现为「没人说话 bot 也接话」。

上面的复现（``C:\\APP\\_vad_probe.py``，改之前）：说 600ms + 静 1200ms
**什么都不吐**；说 2000ms + 静 1200ms 吐 50ms、RMS=0.0000。

这个文件是回归锁：真 VAD、真 PCM，不 stub。
"""

from __future__ import annotations

import struct

from channels import create_vad
from channels.api import VoiceActivityDetector
from channels.models import UTTERANCE_ENERGY_THRESHOLD as _MIN_UTTERANCE_RMS
from channels.models import VadvConfig
from core.audio import pcm16_rms

SR = 16000
FRAME_MS = 30


def _vad(**kw) -> VoiceActivityDetector:
    values = dict(sample_rate=SR, frame_ms=FRAME_MS, silence_ms=700)
    values.update(kw)
    return create_vad(VadvConfig(**values))


def tone(amplitude: float, ms: int) -> bytes:
    """恒定幅度的 PCM16。ms 取 30 的整数倍，好让帧边界对得齐。"""
    return struct.pack("<h", int(amplitude * 32768)) * int(SR * ms / 1000)


def samples(ms: int) -> int:
    """ms 对应多少字节的 PCM16。"""
    return int(SR * ms / 1000) * 2


def feed(vad: VoiceActivityDetector, data: bytes, chunk_bytes: int = 1920) -> list[bytes]:
    """按真实推流的粒度喂，收集切出来的句子。"""
    out: list[bytes] = []
    for i in range(0, len(data), chunk_bytes):
        got = vad.feed(data[i : i + chunk_bytes])
        if got:
            out.append(got)
    return out


# ----------------------------------------------------------------------
# 核心：句子必须装着话
# ----------------------------------------------------------------------


def test_utterance_carries_the_actual_speech() -> None:
    """说 600ms 就得到 600ms 的音频 —— 改之前这里返回的是 50ms 静音。"""
    got = feed(_vad(), tone(0.15, 600) + tone(0.0, 1200))
    assert len(got) == 1, f"应该正好切出一句，实际 {len(got)} 句"
    assert len(got[0]) == samples(600), f"语音被截断：{len(got[0])}B != {samples(600)}B"
    assert pcm16_rms(got[0]) > 0.1, "切出来的不是说话声"


def test_leading_silence_is_not_included() -> None:
    """开口之前的静音不能混进 utterance。"""
    got = feed(_vad(), tone(0.0, 1500) + tone(0.15, 450) + tone(0.0, 1500))
    assert len(got) == 1
    assert len(got[0]) == samples(450)
    assert pcm16_rms(got[0]) > 0.1


def test_trailing_silence_is_trimmed_off() -> None:
    """用来判定「说完了」的那段静音要裁掉：对 ASR 没用，还会把 RMS 拉低。"""
    got = feed(_vad(), tone(0.15, 300) + tone(0.0, 2100))
    assert len(got) == 1
    assert len(got[0]) == samples(300)


# ----------------------------------------------------------------------
# 不能误触发
# ----------------------------------------------------------------------


def test_silence_alone_never_emits() -> None:
    """数字静音（播放器「空场」时推的就是它）永远切不出句子。"""
    assert feed(_vad(), tone(0.0, 5000)) == []


def test_reset_clears_a_half_spoken_utterance() -> None:
    """reset() 之后不能把上一句的残渣带出来。"""
    vad = _vad()
    feed(vad, tone(0.15, 300))  # 话说到一半
    vad.reset()
    assert feed(vad, tone(0.0, 3000)) == []


def test_noise_floor_utterance_is_quiet_enough_for_the_agent_gate() -> None:
    """底噪会一直被当成「有人在说」，直到 max_utterance 强制切句 ——
    但切出来的 RMS 必须低于 agent 的闸门，否则又变成「没人说话也接话」。"""
    got = feed(_vad(), tone(0.015, 16000))
    assert got, "底噪没有被切出来？那这条断言就没意义了"
    assert pcm16_rms(got[-1]) < _MIN_UTTERANCE_RMS


# ----------------------------------------------------------------------
# 边界
# ----------------------------------------------------------------------


def test_real_speech_clears_the_agent_gate() -> None:
    """小声说话（0.03，实测分布的下沿）裁掉尾巴后仍要高过闸门。

    没裁尾巴的话 450ms@0.03 + 720ms@0 只有 0.0179，会被误杀 —— 这条同时
    锁住「裁静音」这个行为。"""
    got = feed(_vad(), tone(0.03, 450) + tone(0.0, 1500))
    assert len(got) == 1
    assert 0.03 > _MIN_UTTERANCE_RMS >= 0.0179, "闸门挪了，这条测试的前提变了"
    assert pcm16_rms(got[0]) > _MIN_UTTERANCE_RMS


def test_max_utterance_ms_forces_a_cut() -> None:
    """一直说个不停时按上限强制切句，不能无限攒。

    上限是「过了就切」，判定在装帧之后，所以每句最多超出一帧（30ms）。
    """
    got = feed(_vad(max_utterance_ms=1000), tone(0.15, 4000))
    assert len(got) >= 3, f"4000ms 只切出 {len(got)} 段"
    assert all(len(u) <= samples(1000 + FRAME_MS) for u in got)


def test_odd_chunk_sizes_do_not_lose_speech() -> None:
    """真实推流的块大小不固定，跨块拼接不能把语音吃掉。"""
    data = tone(0.0, 1500) + tone(0.15, 450) + tone(0.0, 1500)
    got = feed(_vad(), data, chunk_bytes=2002)  # 不是帧长（960B）的整数倍
    assert len(got) == 1
    assert len(got[0]) == samples(450)
