import asyncio

from channels import create_audio_ingress, create_duplex
from channels.models import AudioInputPolicy, VadvConfig


def test_input_authorization_and_sampling_are_preserved_before_vad():
    ingress = create_audio_ingress(AudioInputPolicy(listen_only_uids=("peer",)),
                                    resolve_speaker=lambda uid: "peer" if uid == "123" else uid)
    assert ingress.accept("other", b"\0\1", 16000) is None
    assert ingress.accept("123", b"odd", 16000) is None
    audio = ingress.accept("123", b"\0\1", 16000)
    assert audio.user_key == "peer" and audio.pcm16 == b"\0\1"


def test_input_overflow_reports_a_gap_before_the_next_frame():
    async def scenario():
        class Transport:
            async def start(self): pass
            async def close(self): pass
            def set_remote_pcm_callback(self, callback): self.callback = callback
        transport = Transport()
        duplex = create_duplex(transport)
        records = []
        entered, release = asyncio.Event(), asyncio.Event()
        async def on_audio(uid, pcm, rate):
            records.append(("audio", uid))
            if len(records) == 1:
                entered.set()
                await release.wait()
        duplex.set_remote_pcm_handler(on_audio)
        duplex.set_input_gap_handler(lambda uid:records.append(("gap", uid)))
        await duplex.start()
        try:
            transport.callback("u", b"\0\1", 16000)
            await entered.wait()
            # 按字节总预算溢出（64KiB *65 >4MiB）；不能把缺口拼接为完整一句。
            for _ in range(66):
                transport.callback("u", b"\0\1" * 32768, 16000)
            for _ in range(3):
                await asyncio.sleep(0)
            release.set()
            for _ in range(10):
                await asyncio.sleep(0)
            transport.callback("u", b"\0\1", 16000)
            for _ in range(10):
                await asyncio.sleep(0)
            assert ("gap", "u") in records
            assert records[-2:] == [("gap", "u"), ("audio", "u")]
        finally:
            release.set()
            await duplex.close()
    asyncio.run(scenario())


def test_gap_reset_does_not_join_two_partial_speech_windows():
    policy = AudioInputPolicy(VadvConfig(sample_rate=16000, silence_ms=60), min_utterance_ms=0)
    ingress = create_audio_ingress(policy)
    speech = b"\0\x40"*480
    silence = b"\0\0"*480
    for _ in range(3):
        ingress.cut(ingress.accept("u", speech, 16000))
    ingress.reset()
    for _ in range(3):
        assert ingress.cut(ingress.accept("u", silence, 16000)).status == "accumulating"


def test_many_speakers_cannot_accumulate_unbounded_vad_memory():
    ingress = create_audio_ingress(AudioInputPolicy(min_utterance_ms=0))
    results = []
    for number in range(65):
        audio = ingress.accept(f"member-{number}", b"\0\x40"*32768, 16000)
        results.append(ingress.cut(audio).status)
    assert results[-1] == "gap"
