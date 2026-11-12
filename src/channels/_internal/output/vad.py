"""基于能量的简易 VAD：按帧判断是否有人在说话，并在静音后切出一句。"""

from __future__ import annotations

from channels.models import VadvConfig
from core.audio import pcm16_rms


class EnergyVad:
    def __init__(self, config: VadvConfig | None = None) -> None:
        self.config = config or VadvConfig()
        self._buffer = bytearray()
        self._utterance = bytearray()
        self._voiced_end = 0
        self._speaking = False
        self._silence_ms = 0
        self._utterance_ms = 0

    def reset(self) -> None:
        self._buffer.clear()
        self._utterance.clear()
        self._voiced_end = 0
        self._speaking = False
        self._silence_ms = 0
        self._utterance_ms = 0

    def feed(self, pcm16: bytes) -> bytes | None:
        """喂入 PCM16 小段；若一句结束则返回这一句的 PCM，否则 None。

        切句时返回的是**从第一个有声帧到最后一个有声帧**的音频，末尾那段用来
        判定「说完了」的静音会被裁掉。

        注意 ``_utterance`` 与 ``_buffer`` 是两回事，别用 ``_buffer`` 当结果：
        ``_buffer`` 只是还没凑够一帧的输入暂存区，边读边 ``del``，说话过程中
        一直是接近空的。早期版本直接返回 ``bytes(self._buffer)``，于是每一句
        都只剩最后一帧的静音尾巴 —— 真正的语音被逐帧丢掉，ASR 收到的永远
        是 ~30ms 的静音（对话式 ASR 不会返回空，会照着编一句，表现为「识别
        不准」和「没人说话也接话」）。见 tests/test_vad.py。
        """
        if not pcm16:
            return None
        self._buffer.extend(pcm16)
        frame_bytes = int(self.config.sample_rate * self.config.frame_ms / 1000) * 2
        if frame_bytes <= 0:
            return None

        while len(self._buffer) >= frame_bytes:
            frame = bytes(self._buffer[:frame_bytes])
            del self._buffer[:frame_bytes]
            rms = pcm16_rms(frame)
            active = rms >= self.config.energy_threshold

            if not self._speaking:
                if not active:
                    continue  # 开口之前的静音：丢掉，不进 utterance
                self._speaking = True
                self._silence_ms = 0
                self._utterance_ms = 0

            self._utterance.extend(frame)
            self._utterance_ms += self.config.frame_ms
            if active:
                self._silence_ms = 0
                self._voiced_end = len(self._utterance)
            else:
                self._silence_ms += self.config.frame_ms

            if (
                self._silence_ms >= self.config.silence_ms
                or self._utterance_ms >= self.config.max_utterance_ms
            ):
                utt = bytes(self._utterance[: self._voiced_end])
                self.reset()
                return utt or None
        return None
