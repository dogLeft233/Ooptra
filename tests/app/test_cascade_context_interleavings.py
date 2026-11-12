"""独立验证公开流程的交错顺序；上下文、行为、SQLite和播放协议保持真实。"""
import asyncio

import pytest

from app.flows import run_cascade
from behavior import create_behavior
from behavior.models import ReplyAssessment, ReplySettings, VoiceControlDecision
from channels import create_channel
from channels.models import AudioPushResult, JoinRoom, LeaveRoom, RoomTarget
from context import create_context_service
from context.models import Observation
from dialogue import create_expression_job
from dialogue.models import ControlInterpretation, DialogueCapabilities, Recognition


class Transport:
    def __init__(self):
        self.audio = bytearray()

    async def join(self, target):
        return "rtc"

    async def leave(self):
        pass

    async def retire_output(self):
        pass

    async def push_audio(self, pcm, rate, *, finish=False):
        self.audio.extend(pcm)
        return AudioPushResult(True)

    async def wait_playback(self, timeout):
        return AudioPushResult(True)


class Model:
    def __init__(self, semantic):
        self.semantic = semantic
        self.assessments = []
        self.expressions = []

    def capabilities(self):
        return DialogueCapabilities(semantic_reply=self.semantic)

    async def recognize(self, audio, permit):
        return Recognition(audio.input_ref, audio.session, permit.attempt_id,
                           audio.user_key, "recognized", "请回答我")

    async def interpret_control(self, request):
        return ControlInterpretation("candidate", VoiceControlDecision(leave=True))

    async def assess_reply(self, request, prepared, *, aliases):
        self.assessments.append((request, prepared))
        return ReplyAssessment(request.session, request.input_ref, "addressed")

    def generate(self, intent, prepared, *, tools):
        self.expressions.append((intent, prepared))

        async def produce(emit):
            await emit("text_final", text="回答原说话者")
            await emit("audio", pcm16=b"\0\x40" * 320, sample_rate=16000)

        return create_expression_job(intent.action_id, intent.session, produce,
                                     deadline=intent.deadline)


async def setup_flow(tmp_path, semantic):
    context = create_context_service(str(tmp_path / "history.jsonl"))
    transport, behavior, model = Transport(), create_behavior(), Model(semantic)
    channel = create_channel(transport)
    session = (await channel.execute(JoinRoom("join", RoomTarget("a", "room"), 0))).session
    behavior.start_session(session)
    arguments = dict(backend=model, context=context, behavior=behavior, channel=channel,
                     session=session, trigger_id="alice-input", user_key="alice",
                     pcm=b"\0\x40" * 16000, sample_rate=16000, limit=1,
                     settings=ReplySettings(probability=100, voice_leave_enabled=True),
                     interpret_controls=False, current=lambda: True, on_playing=lambda _: None)
    return context, channel, behavior, model, transport, arguments


@pytest.mark.parametrize("semantic", [False, True])
def test_new_room_fact_before_preparation_does_not_drop_selected_reply(tmp_path, monkeypatch, semantic):
    async def run():
        context, channel, behavior, model, transport, args = await setup_flow(tmp_path, semantic)
        prepare = context.prepare_context

        async def with_new_fact(request):
            # 公开入口前的新事实必须保持真实版本推进与授权检查。
            await context.observe_async(Observation("bob-input", args["session"], "bob-asr", 1,
                "recognized", "我是旁边的说话者", "bob", "bob-input"))
            return await prepare(request)

        monkeypatch.setattr(context, "prepare_context", with_new_fact)
        try:
            result = await run_cascade(**args)
            assert result.outcome.status == "completed"
            assert result.reply_text == "回答原说话者" and transport.audio
            assert behavior.snapshot().attempts == 1
            intent, prepared = model.expressions[0]
            assert (intent.trigger_id, intent.user_key, intent.session) == (
                "alice-input", "alice", args["session"])
            assert prepared.context_ref.session == args["session"]
            assert intent.context_version == prepared.version
            if semantic:
                assert model.assessments[0][1] == prepared
            assert [(row.user_key, row.content) for row in prepared.history] == [
                ("bob", "我是旁边的说话者")]
            assert not prepared.authorized_write_grants
            assert [row["content"] for row in context.history.recent(user_key="alice")] == [
                "请回答我", "回答原说话者"]
        finally:
            await channel.execute(LeaveRoom("cleanup", args["session"]))
            await context.close()
    asyncio.run(run())


@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("interruption", ["cancel", "superseded"])
def test_preparation_interruption_does_not_start_model_or_playback(
        tmp_path, monkeypatch, semantic, interruption):
    async def run():
        context, channel, behavior, model, transport, args = await setup_flow(tmp_path, semantic)
        prepare = context.prepare_context
        entered, release = asyncio.Event(), asyncio.Event()
        current = True
        args["current"] = lambda: current

        async def held_preparation(request):
            result = await prepare(request)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(context, "prepare_context", held_preparation)
        task = asyncio.create_task(run_cascade(**args))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if interruption == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                current = False
                release.set()
                assert (await task).reason == "superseded"
            assert model.assessments == []
            assert model.expressions == [] and not transport.audio
            assert behavior.snapshot().attempts == 1
            assert [row["content"] for row in context.history.recent()] == ["请回答我"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await channel.execute(LeaveRoom("cleanup", args["session"]))
            await context.close()
    asyncio.run(run())


@pytest.mark.parametrize("semantic", [False, True])
def test_priority_leave_does_not_become_reply(tmp_path, semantic):
    async def run():
        context, channel, behavior, model, transport, args = await setup_flow(tmp_path, semantic)
        args["interpret_controls"] = True
        try:
            result = await run_cascade(**args)
            assert result.control.control.leave
            assert result.outcome is None and result.reply_text == ""
            assert model.assessments == [] and model.expressions == [] and not transport.audio
            assert behavior.snapshot().attempts == 1
        finally:
            await channel.execute(LeaveRoom("cleanup", args["session"]))
            await context.close()
    asyncio.run(run())
