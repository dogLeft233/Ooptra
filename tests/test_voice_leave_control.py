"""语音控制通过公开生成任务、真实房间执行和外部音频端口完成离场。"""
import asyncio

import pytest
from support.channel_fixtures import make_voice_agent
from support.live_runtime import Playback, audio_message, make_live_agent, settle

from behavior.models import VoiceControlDecision
from dialogue import create_expression_job
from dialogue.models import ControlInterpretation, DialogueCapabilities, Recognition
from operations import load_voice_agent_settings
from webui import config_editor


class DeparturePlayback(Playback):
    def __init__(self):
        super().__init__()
        self.farewell_drain = asyncio.Event()

    async def wait_tts_complete(self, timeout=30):
        if self.chunks:
            await asyncio.wait_for(self.farewell_drain.wait(), timeout)
        return {'ok': True}


class Model:
    """公开模型端口替身；表达仍使用真实有界任务，磁盘与渠道不替换。"""
    name = 'mimo_cascade'
    def __init__(self, *, leave=True, delay=False, fail=False):
        self.leave = leave
        self.delay = delay
        self.fail = fail
        self.calls = []
        self.generated = asyncio.Event()
        self.release = asyncio.Event()
        self.jobs = set()

    def capabilities(self):
        return DialogueCapabilities()

    def bind_history(self, reader):
        pass

    async def recognize(self, audio, permit):
        self.calls.append('recognize')
        return Recognition(audio.input_ref, audio.session, permit.attempt_id,
                           audio.user_key, 'recognized', '机器人请退出语音')

    async def interpret_control(self, input):
        self.calls.append('interpret')
        return ControlInterpretation('candidate', VoiceControlDecision(leave=self.leave))

    def generate(self, intent, context, *, tools=None):
        async def produce(emit):
            self.calls.append(('generate', intent.kind, intent.text))
            self.generated.set()
            if self.delay:
                await self.release.wait()
            if self.fail:
                raise RuntimeError('synthetic unavailable model')
            await emit('text_final', text='拜拜，我先下了')
            await emit('audio', pcm16=b'\1\0' * 100, sample_rate=24000)
        job = create_expression_job(intent.action_id, intent.session, produce, deadline=intent.deadline)
        self.jobs.add(job)
        return job

    async def aclose(self):
        for job in tuple(self.jobs):
            await job.aclose()
        self.jobs.clear()


async def until(predicate, timeout=2):
    async def wait():
        while not predicate():
            await asyncio.sleep(.002)
    await asyncio.wait_for(wait(), timeout)


async def room_agent(tmp_path, **model_options):
    agent = make_voice_agent(tmp_path)
    await agent.backend.aclose()
    model = Model(**model_options)
    agent.backend = model
    agent.settings.voice_leave_enabled = True
    agent.settings.reply_probability_percent = 0
    playback = DeparturePlayback()
    agent.bind_duplex(playback)
    await agent.join('area', 'room')
    return agent, playback, model


async def spoken(playback, duration_ms=600):
    await playback.capture('member', b'\x00\x20' * (duration_ms * 16) + b'\0\0' * (1300 * 16))


def test_voice_leave_switch_defaults_on_and_can_be_saved_off(monkeypatch):
    import config
    monkeypatch.setattr(config, 'VOICE_AGENT_CONFIG', {})
    settings, _ = load_voice_agent_settings()
    assert settings.voice_leave_enabled is True
    field = config_editor.schema_payload()['voice']['fields']['voice_leave_enabled']
    assert (field['type'], field['tier'], field['value']) == ('bool', 'basic', True)
    monkeypatch.setattr(config, 'VOICE_AGENT_CONFIG', {'voice_leave_enabled': False})
    settings, _ = load_voice_agent_settings()
    assert settings.voice_leave_enabled is False


def test_zero_probability_control_waits_for_real_farewell_playback_before_leaving(tmp_path):
    async def scenario():
        agent, playback, model = await room_agent(tmp_path)
        await spoken(playback)
        await until(lambda: bool(playback.chunks))
        assert model.calls[:2] == ['recognize', 'interpret']
        assert agent.status()['joined'] and agent.test_platform.voice.leaves == 0
        assert model.calls[2][1] == 'announcement'
        playback.farewell_drain.set()
        await until(lambda: not agent.status()['joined'])
        assert agent.test_platform.voice.leaves == 1
        await agent.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('enabled,intent', [(False, True), (True, False)])
def test_disabled_or_non_departure_input_keeps_room(tmp_path, enabled, intent):
    async def scenario():
        agent, playback, model = await room_agent(tmp_path, leave=intent)
        agent.settings.voice_leave_enabled = enabled
        await spoken(playback)
        await until(lambda: 'recognize' in model.calls)
        await settle(100)
        assert ('interpret' in model.calls) is enabled
        assert agent.status()['joined'] and agent.test_platform.voice.leaves == 0
        assert not model.generated.is_set()
        await agent.stop()
    asyncio.run(scenario())


def test_manual_room_change_cancels_departure_and_never_leaves_new_room(tmp_path):
    async def scenario():
        agent, _, model = await room_agent(tmp_path, delay=True)
        original = agent.channels.snapshot().session
        assert agent.request_voice_leave(action_id='departure', expected_session=original)
        assert agent.request_voice_leave(action_id='departure', expected_session=original)
        await asyncio.wait_for(model.generated.wait(), 1)
        await agent.join('other-area', 'other-room')
        model.release.set()
        await settle(100)
        assert agent.status()['joined'] and agent.status()['area'] == 'other-area'
        assert agent.test_platform.voice.leaves == 1
        assert not agent.request_voice_leave(action_id='stale', expected_session=original)
        await agent.stop()
    asyncio.run(scenario())


def test_failed_farewell_does_not_prevent_actual_departure_result(tmp_path):
    async def scenario():
        agent, _, _ = await room_agent(tmp_path, fail=True)
        assert agent.request_voice_leave(action_id='failed-farewell')
        await until(lambda: not agent.status()['joined'])
        assert agent.channels.outcome('failed-farewell').status == 'completed'
        assert agent.test_platform.voice.leaves == 1
        await agent.stop()
    asyncio.run(scenario())


def test_disabling_control_cancels_pending_farewell_and_keeps_room(tmp_path):
    async def scenario():
        agent, _, model = await room_agent(tmp_path, delay=True)
        assert agent.request_voice_leave(action_id='cancelled')
        await asyncio.wait_for(model.generated.wait(), 1)
        agent.settings.voice_leave_enabled = False
        await agent.refresh(['voice_leave_enabled'])
        model.release.set()
        await settle(100)
        assert agent.status()['joined'] and agent.test_platform.voice.leaves == 0
        assert not agent.request_voice_leave()
        await agent.stop()
    asyncio.run(scenario())


def test_failed_departure_keeps_actual_failure_queryable_and_spoken_retry_works(tmp_path):
    async def scenario():
        agent, playback, _ = await room_agent(tmp_path)
        playback.farewell_drain.set()
        agent.test_platform.voice.fail_leave = True
        assert agent.request_voice_leave(action_id='first-failed')
        await until(lambda: agent.channels.outcome('first-failed').status == 'failed')
        await until(lambda: '退房失败' in agent.last_reply)
        assert agent.status()['joined']
        agent.test_platform.voice.fail_leave = False
        await until(lambda: not agent.status()['speaking'])
        await spoken(playback)
        await until(lambda: not agent.status()['joined'])
        assert agent.test_platform.voice.leaves == 1
        assert agent.channels.outcome('first-failed').status == 'failed'
        await agent.stop()
    asyncio.run(scenario())


def test_live_tool_acknowledges_scheduling_then_waits_for_farewell_tail_before_leave(tmp_path):
    async def scenario():
        agent, playback, sockets = await make_live_agent(tmp_path,
            voice_leave_enabled=True, reply_probability_percent=0)
        first = sockets[0]
        await first.deliver({'toolCall': {'functionCalls': [dict(id='leave', name='leave_voice_room', args={})]}})
        await until(lambda: any('toolResponse' in packet for packet in first.sent))
        acknowledgement = next(packet for packet in first.sent if 'toolResponse' in packet)
        reply = acknowledgement['toolResponse']['functionResponses'][0]['response']
        assert reply['status'] == 'scheduled' and reply['ok'] is False
        await until(lambda: len(sockets) >= 2)
        await until(lambda: any('text' in packet.get('realtimeInput', {}) for packet in sockets[-1].sent))
        playback.drain.clear()
        await sockets[-1].deliver(audio_message(text='拜拜，我下了', done=True))
        await until(lambda: bool(playback.chunks))
        assert agent.status()['joined']
        playback.drain.set()
        await until(lambda: not agent.status()['joined'])
        assert all(socket.closed for socket in sockets)
        await agent.stop()
    asyncio.run(scenario())
