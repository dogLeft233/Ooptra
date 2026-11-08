"""真实执行结果决定历史提交，模型草稿不冒充已播放输出。"""
import asyncio

import pytest
from support.channel_fixtures import establish_room
from test_voice_session_regressions import make_agent


@pytest.mark.parametrize("playback_ok", [False, True])
def test_fixed_speech_records_only_after_playback_confirmation(tmp_path, playback_ok):
    async def scenario():
        agent = make_agent(tmp_path, backend="mimo_cascade")
        await establish_room(agent)
        waiting = asyncio.Event()
        confirm = asyncio.Event()
        from dialogue import create_expression_job
        class FixedBackend:
            def generate(self, intent, prepared, **kwargs):
                async def produce(emit):
                    await emit("text_final", text=intent.text)
                    await emit("audio", pcm16=b"\x01\x00" * 16, sample_rate=24000)
                return create_expression_job(intent.action_id, intent.session, produce,
                                             deadline=intent.deadline)
            async def aclose(self):
                pass
        async def drained(timeout=30):
            waiting.set()
            await confirm.wait()
            return {"ok": playback_ok}
        agent.backend = FixedBackend()
        agent.duplex.wait_tts_complete = drained
        task = asyncio.create_task(agent.speak_text("已经完成的回复"))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            assert agent.memory.recent() == []
            confirm.set()
            result = await task
            assert result["ok"] is playback_ok
            assert [row["content"] for row in agent.memory.recent()] == (
                ["已经完成的回复"] if playback_ok else [])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await agent.backend.aclose()

    asyncio.run(scenario())


def test_live_generation_receiver_does_not_wait_for_playback(tmp_path):
    from support.live_runtime import audio_message, make_live_agent

    async def scenario():
        agent, playback, sockets = await make_live_agent(tmp_path)
        blocked = asyncio.Event()
        release = asyncio.Event()
        async def drained(timeout=30):
            blocked.set()
            await release.wait()
            return {"ok": True}
        playback.wait_tts_complete = drained
        received = asyncio.create_task(sockets[0].deliver(audio_message(text="reply", done=True)))
        try:
            await asyncio.wait_for(blocked.wait(), 1)
            await received
            assert received.done(), "模型收包不能等待浏览器播放排空"
            assert not release.is_set()
            assert not [row for row in agent.memory.recent() if row["role"] == "assistant"]
            release.set()
            assert await agent.wait_for_reply_end(1)
            assert agent.memory.recent()[-1]["content"] == "reply"
        finally:
            release.set()
            received.cancel()
            await asyncio.gather(received, return_exceptions=True)
            await agent.stop()
    asyncio.run(scenario())
