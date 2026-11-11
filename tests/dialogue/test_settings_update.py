"""公开生成设置应用能力返回当前后端的生命周期效果。"""
from dataclasses import FrozenInstanceError

import pytest

from dialogue import create_gemini_backend, create_mimo_backend
from operations.models import ReasoningEffort, VoiceAgentSettings


@pytest.mark.parametrize("factory, live", [(create_gemini_backend, True), (create_mimo_backend, False)])
def test_generation_settings_report_deferred_tasks_and_live_invalidation(factory, live):
    settings = VoiceAgentSettings()
    backend = factory(settings)
    fresh = VoiceAgentSettings(deepseek_reasoning_effort=ReasoningEffort.HIGH)
    update = backend.update_settings(fresh, ("deepseek_reasoning_effort", "output_volume"))
    assert update.deferred_fields == frozenset({"deepseek_reasoning_effort"})
    assert not update.reopen_live
    assert backend.settings.deepseek_reasoning_effort == ReasoningEffort.HIGH
    with pytest.raises(FrozenInstanceError):
        update.reopen_live = True
    changed_voice = VoiceAgentSettings(gemini_voice="Puck")
    update = backend.update_settings(changed_voice, ("gemini_voice", "memory_path"))
    assert update.reopen_live is live
    assert update.deferred_fields == frozenset()
    assert backend.settings.gemini_voice == "Puck"
