"""供应商无关的 PCM16 能量测量机制。"""

import struct


def pcm16_rms(pcm16: bytes) -> float:
    if len(pcm16) < 2:
        return 0.0
    count = len(pcm16) // 2
    samples = struct.unpack("<" + "h" * count, pcm16[: count * 2])
    if not samples:
        return 0.0
    acc = 0.0
    for s in samples:
        v = s / 32768.0
        acc += v * v
    return (acc / count) ** 0.5
