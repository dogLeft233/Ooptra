import asyncio

import pytest
from support.channel_fixtures import make_voice_agent

from behavior.models import ControlAction, VoiceControlDecision, VolumeCommand
from channels.models import ActionOutcome
from dialogue.models import ControlInterpretation, Recognition


@pytest.mark.parametrize("kind", ["mute", "leave"])
def test_cascade_control_feedback_uses_registered_action_identity(tmp_path, monkeypatch, kind):
    async def scenario():
        agent = make_voice_agent(tmp_path)
        agent.settings.voice_volume_enabled = True
        agent.settings.voice_leave_enabled = True
        async def recognize(audio, permit):
            return Recognition(audio.input_ref, audio.session, permit.attempt_id,
                               audio.user_key, "recognized", "control")
        async def interpret(control):
            return ControlInterpretation("candidate", VoiceControlDecision(leave=True) if kind == "leave"
                else VoiceControlDecision(volume=VolumeCommand("mute")))
        async def farewell(*args, **kwargs): return {"ok": True}
        async def set_volume(volume): return {"ok": True}
        monkeypatch.setattr(agent.backend, "recognize", recognize)
        monkeypatch.setattr(agent.backend, "interpret_control", interpret)
        monkeypatch.setattr(agent, "speak_announcement", farewell)
        agent.duplex.set_output_volume = set_volume
        actions, feedback = [], []
        decide, observe = agent.behavior.decide, agent.behavior.feedback
        def decided(*args, **kwargs):
            result = decide(*args, **kwargs)
            if isinstance(result, ControlAction):
                actions.append(result)
            return result
        def observed(result):
            receipt = observe(result)
            feedback.append((result, receipt))
            return receipt
        monkeypatch.setattr(agent.behavior, "decide", decided)
        monkeypatch.setattr(agent.behavior, "feedback", observed)
        await agent.join("a", "c")
        try:
            await agent._handle_utterance("original", b"\0\x40" * 8000, 16000)
            if kind == "leave" and agent._voice_control_task is not None:
                await asyncio.wait_for(agent._voice_control_task, 1)
            assert len(actions) == 1
            actual = [(result, receipt) for result, receipt in feedback if result.action_id == actions[0].action_id]
            assert len(actual) == 1
            assert isinstance(actual[0][0], ActionOutcome)
            assert actual[0][0].status == "completed"
            assert actual[0][1].status == "accepted"
            assert agent.channels.outcome(actions[0].action_id) == actual[0][0]
        finally:
            await agent.stop()
    asyncio.run(scenario())
