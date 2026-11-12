"""后台识别更新真实上下文时，已选输入仍使用原身份完成当前回复。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.flows import run_cascade
from app.recognition import PreparedRecognition, RecognitionInputs
from behavior import create_behavior
from behavior.models import AudioReady, ReplyAssessment, ReplySettings, VoiceControlDecision
from channels import create_channel
from channels.models import AudioPushResult, JoinRoom, RoomTarget
from context import create_context_service
from dialogue import create_expression_job
from dialogue.models import ControlInterpretation, DialogueCapabilities, Recognition


@pytest.mark.parametrize("semantic", [False, True])
def test_background_recognition_during_control_keeps_selected_reply(tmp_path, semantic):
    async def run():
        entered, resume = asyncio.Event(), asyncio.Event()
        prepared_inputs = []

        class Transport:
            async def join(self, target): return "rtc"
            async def leave(self): pass
            async def retire_output(self): pass
            async def push_audio(self, pcm, rate, *, finish=False): return AudioPushResult(True)
            async def wait_playback(self, timeout): return AudioPushResult(True)

        class Model:
            def capabilities(self): return DialogueCapabilities(semantic_reply=semantic)
            async def recognize(self, audio, permit):
                return Recognition(audio.input_ref, audio.session, permit.attempt_id,
                                   audio.user_key, "recognized", "请回答这个问题")
            async def interpret_control(self, input):
                entered.set()
                await resume.wait()
                return ControlInterpretation("candidate", VoiceControlDecision())
            async def assess_reply(self, input, prepared, *, aliases):
                return ReplyAssessment(input.session, input.input_ref, "addressed")
            def generate(self, intent, prepared, *, tools):
                prepared_inputs.append(prepared)
                async def produce(emit):
                    await emit("text_final", text="这是回答")
                    await emit("audio", pcm16=b"\0\x40" * 320, sample_rate=16000)
                return create_expression_job(intent.action_id, intent.session, produce,
                                             deadline=intent.deadline)

        context = create_context_service(str(tmp_path / "memory.jsonl"))
        channel, behavior = create_channel(Transport()), create_behavior()
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "voice"), 0))).session
        behavior.start_session(session)
        host = SimpleNamespace(context_service=context,
                               observations=SimpleNamespace(trace=lambda *args, **kwargs: None))
        inputs = RecognitionInputs(host)
        task = asyncio.create_task(run_cascade(
            backend=Model(), context=context, behavior=behavior, channel=channel,
            session=session, trigger_id="selected", user_key="alice", pcm=b"\0\x40" * 16000,
            sample_rate=16000, limit=0, settings=ReplySettings(probability=100),
            interpret_controls=True, current=lambda: True, on_playing=lambda _: None))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            trigger = AudioReady("background", session, "bob", context.snapshot(session.room_target).revision)
            permit = behavior.admit(trigger, 0)
            result = asyncio.get_running_loop().create_future()
            result.set_result(Recognition("background", session, permit.attempt_id, "bob",
                                          "recognized", "我补充一下背景"))
            item = PreparedRecognition(trigger, permit, SimpleNamespace(result=lambda: result), 16000)
            await inputs._watch(item)
            resume.set()
            reply = await asyncio.wait_for(task, 2)
            assert reply.outcome.status == "completed"
            assert reply.reply_text == "这是回答"
            prepared = prepared_inputs[0]
            assert prepared.context_ref.session == session
            assert [(row.user_key, row.content) for row in prepared.history] == [("bob", "我补充一下背景")]
            assert [row["content"] for row in context.history.recent()] == [
                "请回答这个问题", "我补充一下背景", "这是回答"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await context.close()
    asyncio.run(run())

