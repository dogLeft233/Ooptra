"""真实行为/上下文/生成任务/播放协作，仅替代外部识别模型和音频传输。"""
import asyncio

from app.flows import run_cascade
from behavior import create_behavior
from behavior.models import ReplySettings
from channels import create_channel
from channels.models import AudioPushResult, JoinRoom, RoomTarget
from context import create_context_service
from dialogue import create_expression_job
from dialogue.models import Recognition


def test_completed_expression_is_committed_once_after_actual_playback(tmp_path):
    async def scenario():
        class Transport:
            async def join(self, target): return "rtc"
            async def leave(self): pass
            async def retire_output(self): pass
            async def push_audio(self, pcm, rate, *, finish=False): return AudioPushResult(True)
            async def wait_playback(self, timeout): return AudioPushResult(True)
        class ExternalModel:
            async def recognize(self, audio, permit):
                return Recognition(audio.input_ref, audio.session, permit.attempt_id, audio.user_key, "recognized", "你好")
            def generate(self, intent, prepared, *, tools):
                assert prepared.persona == "简短回答"
                async def produce(emit):
                    await emit("text_final", text="你好呀")
                    await emit("audio", pcm16=b"\x00\x40"*320, sample_rate=16000)
                return create_expression_job(intent.action_id, intent.session, produce, deadline=intent.deadline)
        context = create_context_service(str(tmp_path / "history.jsonl"))
        context.history.set_persona("简短回答")
        channel, behavior = create_channel(Transport()), create_behavior()
        session = (await channel.execute(JoinRoom("join", RoomTarget("a", "b"), 0))).session
        behavior.start_session(session)
        try:
            result = await run_cascade(backend=ExternalModel(), context=context, behavior=behavior,
                channel=channel, session=session, trigger_id="input", user_key="u", pcm=b"\x00\x40"*320,
                sample_rate=16000, limit=3, settings=ReplySettings(probability=100),
                interpret_controls=False, current=lambda: True, on_playing=lambda _:None)
            assert result.outcome.status == "completed"
            assert result.reply_text == "你好呀"
            assert [row["content"] for row in context.history.recent()] == ["你好", "你好呀"]
            assert behavior.snapshot().attempts == 1
        finally:
            await context.close()
    asyncio.run(scenario())
