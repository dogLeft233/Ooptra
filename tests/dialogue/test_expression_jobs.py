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



def test_queued_timing_survives_closing_audio_after_the_first_frame():
    async def run():
        produced, closed = asyncio.Event(), asyncio.Event()
        async def produce(emit):
            try:
                await emit("audio", pcm16=b"\x01\x00")
                await emit("timing", timings_ms=(("tts_total", 12.0),))
                produced.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
        job = create_expression_job("action", session(), produce, deadline=time.monotonic() + 5)
        output = audio_output(job)
        frames = output.frames()
        assert (await anext(frames)).pcm16 == b"\x01\x00"
        await produced.wait()
        await frames.aclose()
        assert closed.is_set()
        assert dict(job.snapshot().timings_ms)["tts_total"] == 12.0
        assert dict(output.evidence().timings_ms)["tts_total"] == 12.0
        assert output.evidence().status == "cancelled"
    asyncio.run(run())



def test_job_timing_snapshot_remains_bounded_and_keeps_existing_stage_updates():
    async def run():
        produced = asyncio.Event()
        async def produce(emit):
            for index in range(40):
                await emit("timing", timings_ms=((f"stage_{index}", float(index)),))
            await emit("timing", timings_ms=(("stage_0", 99.0),))
            produced.set()
            await asyncio.Event().wait()
        job = create_expression_job("action", session(), produce, deadline=time.monotonic() + 5, queue_size=64)
        await produced.wait()
        await job.aclose()
        timings = dict(job.snapshot().timings_ms)
        assert len(timings) <= 32
        assert timings["stage_0"] == 99.0
        assert "generation_total" in timings
        assert dict(audio_output(job).evidence().timings_ms) == timings
    asyncio.run(run())


@pytest.mark.parametrize("close_early", [False, True])
def test_final_job_log_matches_normal_completion_or_prestart_cancel(caplog, close_early):
    async def run():
        async def produce(emit):
            await emit("audio", pcm16=b"\x01\x00")
        with caplog.at_level("INFO"):
            job = create_expression_job("action", session(), produce, deadline=time.monotonic() + 5)
            if close_early:
                await job.aclose()
            else:
                _ = [frame async for frame in audio_output(job).frames()]
            await job.aclose()
        expected = "cancelled" if close_early else "completed"
        assert job.snapshot().status == expected
        logs = [record.getMessage() for record in caplog.records if "event=voice.generation.finished" in record.getMessage()]
        assert len(logs) == 1 and f"status={expected}" in logs[0]
    asyncio.run(run())
