"""任务流经过真实适配，外部模型服务可由测试注入。"""
import asyncio
import time

import pytest

from channels.models import RoomTarget, SessionRef
from dialogue import audio_output, create_expression_job


def session():
    return SessionRef("run", "binding", "room", RoomTarget("area", "channel"))


def test_job_audio_is_single_consumer_and_splits_large_provider_blocks():
    async def scenario():
        async def produce(emit):
            await emit("text_final", text="你好")
            await emit("audio", pcm16=b"\0\0" * 40000, sample_rate=24000)
        job = create_expression_job("action", session(), produce, deadline=time.monotonic() + 5)
        audio = audio_output(job)
        frames = [frame async for frame in audio.frames()]
        assert [frame.frame_seq for frame in frames] == [0, 1]
        assert b"".join(frame.pcm16 for frame in frames) == b"\0\0" * 40000
        assert audio.evidence().status == "completed"
        assert audio.evidence().text == "你好"
        with pytest.raises(RuntimeError, match="consumer"):
            _ = [event async for event in job.events()]
    asyncio.run(scenario())


def test_close_unblocks_full_producer_and_retains_cancelled_evidence():
    async def scenario():
        closed = asyncio.Event()
        async def produce(emit):
            try:
                for _ in range(100):
                    await emit("audio", pcm16=b"\0\0" * 1024, sample_rate=24000)
            finally:
                closed.set()
        job = create_expression_job("action", session(), produce, deadline=time.monotonic() + 5,
                                    queue_size=1)
        await asyncio.sleep(0)
        await job.aclose()
        assert closed.is_set()
        assert job.snapshot().status == "cancelled"
        audio = audio_output(job)
        assert audio.evidence().status == "cancelled"
    asyncio.run(scenario())


def test_provider_error_is_source_failure_instead_of_successful_eos():
    async def scenario():
        from channels.models import OutputSourceError
        async def produce(emit):
            await emit("audio", pcm16=b"\0\0", sample_rate=24000)
            raise RuntimeError("provider secret body")
        audio = audio_output(create_expression_job("action", session(), produce,
                             deadline=time.monotonic() + 5))
        with pytest.raises(OutputSourceError):
            _ = [frame async for frame in audio.frames()]
        assert audio.evidence().status == "failed"
        assert "secret" not in audio.evidence().reason
    asyncio.run(scenario())


def test_closing_output_before_first_frame_cancels_its_provider():
    async def scenario():
        started = asyncio.Event()
        closed = asyncio.Event()
        async def produce(emit):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        job = create_expression_job("action", session(), produce, deadline=time.monotonic()+5)
        audio = audio_output(job)
        await started.wait()
        frames = audio.frames()
        await frames.aclose()
        assert closed.is_set()
        assert audio.evidence().status == "cancelled"
    asyncio.run(scenario())
