"""真实行为/SQLite/outbox证明无输出终态仍采集原输入。"""
import asyncio

import pytest
from support.context_fixtures import prepare_source

from app.flows import run_cascade
from behavior import create_behavior
from behavior.models import ReplySettings, VoiceControlDecision
from channels import create_channel
from channels.models import JoinRoom, RoomTarget
from context import create_context_service
from dialogue.models import ControlInterpretation, Recognition


@pytest.mark.parametrize("outcome", ["silent", "control", "classification_failed", "generation_failed"])
def test_cascade_terminal_without_playback_collects_original_input_once(tmp_path, outcome):
    async def run():
        class Transport:
            async def join(self, target): return "rtc"
            async def leave(self): pass
            async def retire_output(self): pass
        class Model:
            async def recognize(self, audio, permit):
                return Recognition(audio.input_ref, audio.session, permit.attempt_id, audio.user_key, "recognized", "原始问题")
            async def interpret_control(self, input):
                return ControlInterpretation("unavailable", reason="provider_failed") if outcome == "classification_failed" else ControlInterpretation("candidate", VoiceControlDecision(leave=True))
            def generate(self, *args, **kwargs): raise OSError("provider_failed")
        context = create_context_service(str(tmp_path / "history.jsonl"))
        await prepare_source(context.bridge, {"ok": True, "provider": "memory_companion",
            "memory_scopes": [{"umo": "original"}]}, "area", "room")
        channel, behavior = create_channel(Transport()), create_behavior()
        session = (await channel.execute(JoinRoom("join", RoomTarget("area", "room"), 0))).session
        behavior.start_session(session)
        try:
            try:
                await run_cascade(backend=Model(), context=context, behavior=behavior,
                    channel=channel, session=session, trigger_id="input", user_key="oopz:original",
                    pcm=b"\0\x40" * 16000, sample_rate=16000, limit=3,
                    settings=ReplySettings(probability=0 if outcome == "silent" else 100, voice_leave_enabled=True),
                    interpret_controls=outcome in {"control", "classification_failed"},
                    current=lambda:True, on_playing=lambda _:None)
            except OSError:
                assert outcome == "generation_failed"
            await asyncio.sleep(0)
            events = await context.bridge.poll(wait=0)
            assert len(events) == 1
            assert events[0]["payload"]["user_text"] == "原始问题"
            assert events[0]["payload"]["speaker"] == "oopz:original"
            assert events[0]["payload"]["reply_text"] == ""
            context.finish_input("input")
            assert len(await context.bridge.poll(wait=0)) == 0
            assert context.history.as_messages() == [{"role": "user", "content": "原始问题"}]
        finally:
            await context.close()
    asyncio.run(run())
