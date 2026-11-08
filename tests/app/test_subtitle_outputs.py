"""音频与字幕独立验收，真实任务/渠道/SQLite协作，仅替代外部模型及传输。"""
import asyncio

import pytest

from app.flows import run_cascade
from behavior import create_behavior
from behavior.models import ReplySettings
from channels import create_channel
from channels.models import AudioPushResult, JoinRoom, RoomTarget
from context import create_context_service
from dialogue import create_expression_job
from dialogue.models import Recognition


class Transport:
    def __init__(self, audio_ok):
        self.audio_ok = audio_ok
        self.sent = []
    async def join(self, target): return "rtc"
    async def leave(self): pass
    async def retire_output(self): pass
    async def push_audio(self, pcm, rate, *, finish=False): return AudioPushResult(True)
    async def wait_playback(self, timeout): return AudioPushResult(self.audio_ok, "" if self.audio_ok else "drain_failed")
    async def text_target_exists(self, target): return target == RoomTarget("a", "text")
    async def send_text(self, target, text):
        self.sent.append((target, text))
        return "message-1"


class Model:
    async def recognize(self, audio, permit):
        return Recognition(audio.input_ref, audio.session, permit.attempt_id, audio.user_key, "recognized", "你好")
    def generate(self, intent, prepared, *, tools):
        async def produce(emit):
            await emit("text_final", text="你好呀")
            await emit("audio", pcm16=b"\0\x40"*320, sample_rate=16000)
        return create_expression_job(intent.action_id, intent.session, produce, deadline=intent.deadline)


@pytest.mark.parametrize("audio_ok", [True, False])
def test_subtitle_and_audio_have_distinct_results_and_one_confirmed_assistant(tmp_path, audio_ok):
    async def run():
        transport = Transport(audio_ok)
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "voice"), 0))).session
        context = create_context_service(str(tmp_path / "memory.jsonl"))
        from tests.support.context_fixtures import prepare_source
        await prepare_source(context.bridge, {"ok": True, "provider": "memory", "memory_scopes": [{"umo": "group"}]}, "a", "voice")
        behavior = create_behavior()
        behavior.start_session(session)
        try:
            result = await run_cascade(backend=Model(), context=context, behavior=behavior, channel=channel,
                session=session, trigger_id="input", user_key="u", pcm=b"\0\x40"*16000, sample_rate=16000,
                limit=3, settings=ReplySettings(), interpret_controls=False, current=lambda:True,
                on_playing=lambda _:None, reply_text_to_channel=True, subtitle_target=RoomTarget("a", "text"))
            assert result.outcome.status == ("completed" if audio_ok else "partial")
            assert result.text_outcome.status == "completed"
            assert result.text_outcome.action_id != result.outcome.action_id
            assert result.text_outcome.parent_action_id == result.outcome.action_id
            assert result.text_outcome.media == "text"
            assert transport.sent == [(RoomTarget("a", "text"), "你好呀")]
            assert context.history.as_messages() == [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀"}]
            assert channel.outcome(result.text_outcome.action_id) == result.text_outcome
            writes = await context.bridge.poll(wait=0)
            assert len(writes) == 1
            assert writes[0]["payload"]["user_text"] == "你好"
            assert writes[0]["payload"]["reply_text"] == "你好呀"
        finally:
            await context.close()
    asyncio.run(run())


def test_missing_explicit_text_target_keeps_audio_success_and_explains_subtitle_not_executed(tmp_path):
    async def run():
        transport = Transport(True)
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "voice"), 0))).session
        context = create_context_service(str(tmp_path / "memory.jsonl"))
        behavior = create_behavior()
        behavior.start_session(session)
        try:
            result = await run_cascade(backend=Model(), context=context, behavior=behavior, channel=channel,
                session=session, trigger_id="input", user_key="u", pcm=b"\0\x40"*16000, sample_rate=16000,
                limit=3, settings=ReplySettings(), interpret_controls=False, current=lambda:True,
                on_playing=lambda _:None, reply_text_to_channel=True, subtitle_target=None)
            assert result.outcome.status == "completed"
            assert result.text_outcome is None and result.text_reason == "target_unavailable"
            assert transport.sent == []
            assert len(context.history.as_messages()) == 2
        finally:
            await context.close()
    asyncio.run(run())


@pytest.mark.parametrize("audio_ok", [True, False])
def test_fixed_expression_keeps_audio_and_text_evidence_separate(tmp_path, audio_ok):
    from app.flows import run_expression
    from tests.support.expression_runtime import ScriptedExpression
    async def run():
        transport = Transport(audio_ok)
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "voice"), 0))).session
        context = create_context_service(str(tmp_path / "memory.jsonl"))
        try:
            result = await run_expression(backend=ScriptedExpression(), context=context, channel=channel,
                session=session, text="hello", current=lambda:True,
                reply_text_to_channel=True, subtitle_target=RoomTarget("a", "text"))
            assert result.outcome.status == ("completed" if audio_ok else "partial")
            assert result.text_outcome.status == "completed"
            assert context.history.as_messages() == [{"role": "assistant", "content": "hello"}]
        finally:
            await context.close()
    asyncio.run(run())


def test_live_expression_uses_explicit_subtitle_target_and_records_reply_once(tmp_path):
    from support.live_runtime import audio_message, make_live_agent
    async def run():
        agent, playback, sockets = await make_live_agent(tmp_path,
            reply_text_to_channel=True, subtitle_target=RoomTarget("area", "text"))
        try:
            await sockets[0].deliver(audio_message(user_text="hello", text="hi", done=True))
            assert await agent.wait_for_reply_end(timeout=1)
            assert playback.text_messages == [("hi", "area", "text")]
            assert agent.memory.as_messages() == [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        finally:
            await agent.leave()
    asyncio.run(run())


def test_uncertain_subtitle_does_not_erase_successful_audio_or_repeat_history(tmp_path):
    class TextFailure(Transport):
        async def send_text(self, target, text):
            self.sent.append((target, text))
            raise OSError("external acknowledgement lost")
    async def run():
        transport = TextFailure(True)
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "voice"), 0))).session
        context = create_context_service(str(tmp_path / "memory.jsonl"))
        behavior = create_behavior()
        behavior.start_session(session)
        try:
            result = await run_cascade(backend=Model(), context=context, behavior=behavior, channel=channel,
                session=session, trigger_id="input", user_key="u", pcm=b"\0\x40"*16000, sample_rate=16000,
                limit=3, settings=ReplySettings(), interpret_controls=False, current=lambda:True,
                on_playing=lambda _:None, reply_text_to_channel=True, subtitle_target=RoomTarget("a", "text"))
            assert result.outcome.status == "completed"
            assert result.text_outcome.status == "failed" and result.text_reason == "effects_unconfirmed"
            assert result.subtitle["status"] == "failed"
            assert context.history.as_messages() == [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀"}]
            assert len(transport.sent) == 1
        finally:
            await context.close()
    asyncio.run(run())


def test_cancel_during_subtitle_preserves_audio_fact_and_original_input_once(tmp_path):
    class PendingText(Transport):
        def __init__(self):
            super().__init__(True)
            self.entered = asyncio.Event()
        async def send_text(self, target, text):
            self.sent.append((target, text))
            self.entered.set()
            await asyncio.Event().wait()
    class TrackedModel(Model):
        def generate(self, intent, prepared, *, tools):
            self.action_id = intent.action_id
            return super().generate(intent, prepared, tools=tools)
    async def run():
        transport, model = PendingText(), TrackedModel()
        channel = create_channel(transport)
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "voice"), 0))).session
        context = create_context_service(str(tmp_path / "memory.jsonl"))
        behavior = create_behavior()
        behavior.start_session(session)
        task = asyncio.create_task(run_cascade(backend=model, context=context, behavior=behavior, channel=channel,
            session=session, trigger_id="input", user_key="u", pcm=b"\0\x40"*16000, sample_rate=16000,
            limit=3, settings=ReplySettings(), interpret_controls=False, current=lambda:True,
            on_playing=lambda _:None, reply_text_to_channel=True, subtitle_target=RoomTarget("a", "text")))
        try:
            await transport.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert channel.playback(model.action_id).status == "completed"
            assert channel.outcome(model.action_id + ":text").status == "cancelled"
            assert context.history.as_messages() == [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀"}]
            assert len(transport.sent) == 1
        finally:
            await context.close()
    asyncio.run(run())
