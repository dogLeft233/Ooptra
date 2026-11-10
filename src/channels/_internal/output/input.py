"""监听授权、采样验证与有界逐成员切句，不执行接话策略或模型请求。"""

import logging
from dataclasses import replace

from channels._internal.output.vad import EnergyVad
from channels.models import AudioInputPolicy, IncomingAudio, UtteranceResult
from core.audio import pcm16_rms

logger = logging.getLogger(__name__)


class AudioIngress:
    def __init__(self, policy: AudioInputPolicy, resolve_speaker):
        self._policy = policy
        self._resolve = resolve_speaker
        self._buffers = {}
        self._sequence = 0
        self._generation = 0

    def configure(self, policy: AudioInputPolicy) -> None:
        if policy != self._policy:
            self.reset()
        self._policy = policy

    @property
    def generation(self) -> int:
        return self._generation

    def reset(self) -> None:
        self._generation += 1
        self._buffers.clear()

    def accept(self, uid: str, pcm: bytes, sample_rate: int) -> IncomingAudio | None:
        if (type(pcm) is not bytes or not pcm or len(pcm) > 65536 or len(pcm) % 2
                or type(sample_rate) is not int or not 8000 <= sample_rate <= 96000):
            self.reset()
            return None
        uid = self._resolve(uid)
        if self._policy.listen_only_uids and uid not in self._policy.listen_only_uids:
            return None
        self._sequence += 1
        return IncomingAudio(uid, pcm, sample_rate, self._sequence)

    def cut(self, audio: IncomingAudio) -> UtteranceResult:
        entry = self._buffers.get(audio.user_key)
        if entry is None or entry[0] != audio.sample_rate:
            if len(self._buffers) >= 128:
                self.reset()
                return UtteranceResult("gap")
            entry = (audio.sample_rate, EnergyVad(replace(self._policy.vad, sample_rate=audio.sample_rate)))
            self._buffers[audio.user_key] = entry
        pcm = entry[1].feed(audio.pcm16)
        if sum(vad.buffered_bytes for _, vad in self._buffers.values()) > 4 * 1024 * 1024:
            self.reset()
            return UtteranceResult("gap")
        if pcm is None:
            return UtteranceResult("accumulating", stream_pcm16=entry[1].stream_pcm16)
        energy = pcm16_rms(pcm)
        if energy < self._policy.energy_threshold:
            logger.debug("event=channel.input.noise energy=%.4f samples=%d", energy, len(pcm)//2)
            return UtteranceResult("noise")
        if len(pcm) / 2 / audio.sample_rate * 1000 < self._policy.min_utterance_ms:
            return UtteranceResult("too_short")
        return UtteranceResult("ready", pcm, entry[1].stream_pcm16)
