"""解析后的指令在直接构造及传递中保持业务不变量。"""

from dataclasses import FrozenInstanceError

import pytest

from voice_agent.voice_control import (
    VoiceControlDecision,
    VolumeCommand,
    parse_control_decision,
    parse_volume_command,
)


@pytest.mark.parametrize("action,value", [
    ("unknown", None), (None, None), (True, None),
    ("set", None), ("set", True), ("set", "50"), ("set", 50.0),
    ("set", 9), ("set", 101), ("mute", 50), ("restore", 0),
    ("increase", 10), ("decrease", 10),
])
def test_direct_volume_construction_rejects_invalid_states(action, value):
    # 模拟未类型化调用方绕过解析器；构造器也必须拒绝非法状态。
    with pytest.raises(ValueError):
        VolumeCommand(action, value)


@pytest.mark.parametrize("leave,volume", [
    (0, None), (1, None), ("false", None),
    (False, {"action": "mute"}), (True, VolumeCommand("mute")),
])
def test_direct_decision_construction_rejects_invalid_states(leave, volume):
    with pytest.raises(ValueError):
        VoiceControlDecision(leave, volume)


def test_parsed_command_is_independent_of_mutable_input():
    raw = {"leave": False, "volume": {"action": "set", "value": 50}}
    decision = parse_control_decision(raw)
    raw["volume"]["value"] = 0
    raw["leave"] = True
    assert decision == VoiceControlDecision(volume=VolumeCommand("set", 50))
    with pytest.raises(FrozenInstanceError):
        decision.volume.value = 0
    with pytest.raises(FrozenInstanceError):
        decision.leave = True


def test_valid_absence_is_distinct_from_invalid_input():
    assert parse_volume_command(None) is None
    assert parse_control_decision({"leave": False}) == VoiceControlDecision()
    with pytest.raises(ValueError):
        parse_volume_command({})
    with pytest.raises(ValueError):
        parse_control_decision({})
