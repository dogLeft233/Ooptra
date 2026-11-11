"""控制候选由真实行为授权，不能等待无关的普通接话模型请求。"""
import asyncio

import pytest

from app.flows import run_cascade
from behavior import create_behavior
from behavior.models import (
    ControlProposal,
    ReplyAssessment,
    ReplySettings,
    VoiceControlDecision,
    VolumeCommand,
)
from channels import create_channel
from channels.models import AudioPushResult, JoinRoom, RoomTarget
from context import create_context_service
from dialogue.models import ControlInterpretation, DialogueCapabilities, Recognition


@pytest.mark.parametrize('kind', ['leave', 'volume'])
@pytest.mark.parametrize('mode', ['enabled', 'disabled', 'expired', 'capacity'])
def test_control_authorization_precedes_optional_reply_assessment(tmp_path, kind, mode):
    async def run():
        called = asyncio.Event()
        clock = [1.0]
        candidate = VoiceControlDecision(leave=True) if kind == 'leave' else VoiceControlDecision(volume=VolumeCommand('mute'))
        class Transport:
            async def join(self, target): return 'rtc'
            async def leave(self): pass
            async def retire_output(self): pass
            async def push_audio(self, pcm, rate, *, finish=False): return AudioPushResult(True)
        class Model:
            def capabilities(self): return DialogueCapabilities(semantic_reply=True)
            async def recognize(self, audio, permit):
                return Recognition(audio.input_ref, audio.session, permit.attempt_id, audio.user_key, 'recognized', '请执行控制')
            async def interpret_control(self, input):
                if mode == 'expired':
                    clock[0] = 62
                return ControlInterpretation('candidate', candidate)
            async def assess_reply(self, input, prepared, *, aliases):
                called.set()
                if mode != 'disabled':
                    await asyncio.Event().wait()
                return ReplyAssessment(input.session, input.input_ref, 'silent')
        context = create_context_service(str(tmp_path / 'memory.jsonl'))
        channel, behavior = create_channel(Transport()), create_behavior(clock=lambda: clock[0])
        session = (await channel.execute(JoinRoom('join', RoomTarget('a', 'b'), 0))).session
        behavior.start_session(session)
        settings = ReplySettings(probability=0, voice_leave_enabled=mode != 'disabled', voice_volume_enabled=mode != 'disabled')
        if mode == 'capacity':
            for number in range(1024):
                behavior.decide_control(ControlProposal('other-' + str(number), session, candidate), settings)
        task = asyncio.create_task(run_cascade(backend=Model(), context=context, behavior=behavior, channel=channel,
            session=session, trigger_id='input', user_key='alice', pcm=b'\0\x40' * 16000, sample_rate=16000,
            limit=0, settings=settings, interpret_controls=True, current=lambda: True, on_playing=lambda _: None))
        assessment = asyncio.create_task(called.wait())
        try:
            await asyncio.wait_for(asyncio.wait({task, assessment}, return_when=asyncio.FIRST_COMPLETED), 1)
            assert called.is_set() == (mode == 'disabled'), 'control handling waited for ordinary reply classification'
            result = await asyncio.wait_for(task, 1)
            if mode == 'enabled':
                assert result.control is not None and result.control.control == candidate
            else:
                assert result.control is None
                assert result.reason == {'disabled': 'semantic_silent', 'expired': 'expired_attempt', 'capacity': 'capacity'}[mode]
        finally:
            task.cancel()
            assessment.cancel()
            await asyncio.gather(task, assessment, return_exceptions=True)
            await context.close()
    asyncio.run(run())
