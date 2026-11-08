"""抢话策略通过公开音频活动输入验证，不依赖PCM或模型实现。"""
from behavior import create_barge_in_gate
from behavior.models import SpeechActivity


def test_continuous_speech_interrupts_once_until_rearmed():
    gate = create_barge_in_gate()
    assert not gate.observe(SpeechActivity("u", True, 200), hold_ms=300)
    assert gate.observe(SpeechActivity("u", True, 100), hold_ms=300)
    assert not gate.observe(SpeechActivity("u", True, 1000), hold_ms=300)
    gate.rearm()
    assert not gate.observe(SpeechActivity("u", True, 100), hold_ms=300)


def test_silence_resets_only_that_speakers_continuous_duration():
    gate = create_barge_in_gate()
    assert not gate.observe(SpeechActivity("a", True, 200), hold_ms=300)
    assert not gate.observe(SpeechActivity("b", True, 200), hold_ms=300)
    assert not gate.observe(SpeechActivity("a", False, 20), hold_ms=300)
    assert not gate.observe(SpeechActivity("a", True, 200), hold_ms=300)
    assert gate.observe(SpeechActivity("b", True, 100), hold_ms=300)


def test_zero_hold_keeps_compatibility_first_frame_permission():
    gate = create_barge_in_gate()
    assert gate.observe(SpeechActivity("a", False, 20), hold_ms=0)
    assert not gate.armed
    assert not gate.observe(SpeechActivity("b", True, 200), hold_ms=0)


def test_invalid_audio_activity_never_becomes_policy_evidence():
    import pytest

    for duration in [-1, float("nan"), float("inf"), True]:
        with pytest.raises(ValueError):
            SpeechActivity("a", True, duration)
    with pytest.raises(ValueError):
        SpeechActivity("", True, 20)
    with pytest.raises(ValueError):
        SpeechActivity("a", "yes", 20)
