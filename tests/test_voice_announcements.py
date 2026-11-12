import asyncio

import pytest
from support.expression_runtime import ScriptedExpression
from test_voice_operations import make_agent as base_agent


def make_agent(tmp_path):
    agent = base_agent(tmp_path)
    agent.backend = ScriptedExpression()
    return agent


class AnnouncementDuplex:
    def __init__(self):
        self.audio = []
        self.drained = asyncio.Event()

    async def enable_listen(self, enabled=True):
        return {"ok": True}

    async def push_tts_pcm(self, pcm, rate, *, finish=False):
        self.audio.append(pcm)
        return {"ok": True}

    async def stop_tts(self):
        self.audio.clear()
        return {"ok": True}

    async def wait_tts_complete(self, timeout):
        await asyncio.wait_for(self.drained.wait(), timeout)
        return {"ok": True}


def test_mimo_every_request_rewrites_and_waits_for_tail(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        duplex = AnnouncementDuplex()
        agent.duplex = duplex
        await agent.join("a", "c", source="auto", visit_id="v")
        requests = []

        async def rewrite(kind, template):
            requests.append((kind, template))
            return "大家在玩什么呀？"

        async def tts(text):
            assert text == "大家在玩什么呀？"
            return b"\x00\x01" * 100, 24000

        agent.backend.rewrite_announcement = rewrite
        agent.backend.tts = tts
        task = asyncio.create_task(agent.speak_announcement("enter", "在玩什么", expected_visit_id="v"))
        await asyncio.sleep(0.04)
        assert not task.done()
        assert agent._announcement_task is not None
        duplex.drained.set()
        assert (await task)["ok"]
        assert (await agent.speak_announcement("leave", "再见", expected_visit_id="v"))["ok"]
        assert requests == [("enter", "在玩什么"), ("leave", "再见")]
        assert not agent.last_user_text
        await agent.leave()
    asyncio.run(run())


def test_empty_ai_result_and_timeout_restore_input(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")

        async def rewrite(kind, template):
            return ""

        agent.backend.rewrite_announcement = rewrite
        result = await agent.speak_announcement("enter", "hi", expected_visit_id="v")
        assert not result["ok"] and result["error"]
        assert not agent._announcement_active
        await agent.leave()
    asyncio.run(run())


def test_manual_join_cancels_generation_and_old_audio(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")
        began = asyncio.Event()

        async def rewrite(kind, template):
            began.set()
            await asyncio.Event().wait()

        agent.backend.rewrite_announcement = rewrite
        task = asyncio.create_task(agent.speak_announcement("enter", "hi", expected_visit_id="v"))
        await began.wait()
        # Python 3.10 Task has no cancelling(); keep the runtime contract explicit.
        original_current = asyncio.current_task
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "current_task", lambda: object())
            assert (await asyncio.wait_for(agent.join("b", "d"), 1))["ok"]
            assert not (await task)["ok"]
        assert asyncio.current_task is original_current
        assert agent.status()["area"] == "b"
        assert not agent._announcement_active
        await agent.leave()
    asyncio.run(run())


def test_announcement_caller_cancellation_propagates_and_cleans_generation(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")
        began, disposed = asyncio.Event(), asyncio.Event()

        async def rewrite(kind, template):
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                disposed.set()

        agent.backend.rewrite_announcement = rewrite
        task = asyncio.create_task(agent.speak_announcement("enter", "hi", expected_visit_id="v"))
        await began.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert disposed.is_set()
        assert not agent._announcement_active
        assert agent._announcement_task is None
        await agent.leave()
    asyncio.run(run())


def test_simultaneous_child_and_caller_cancellation_is_not_swallowed(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")
        began = asyncio.Event()

        async def rewrite(kind, template):
            began.set()
            await asyncio.Event().wait()

        agent.backend.rewrite_announcement = rewrite
        caller = asyncio.create_task(agent.speak_announcement("enter", "hi", expected_visit_id="v"))
        await began.wait()
        child = agent._announcement_task
        child.add_done_callback(lambda _: caller.cancel())
        child.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not agent._announcement_active
        assert agent._announcement_task is None
        await agent.leave()
    asyncio.run(run())












def test_announcement_does_not_mix_with_an_existing_reply_tail(tmp_path):
    async def run():
        agent = make_agent(tmp_path)
        duplex = AnnouncementDuplex()
        agent.duplex = duplex
        await agent.join("a", "c", source="auto", visit_id="v")
        rewrites = []

        async def rewrite(kind, template):
            rewrites.append(template)
            return "大家好"

        async def tts(text):
            return b"\x00\x01", 24000

        agent.backend.rewrite_announcement = rewrite
        agent.backend.tts = tts
        task = asyncio.create_task(agent.speak_announcement("enter", "hi", expected_visit_id="v"))
        await asyncio.sleep(0.02)
        assert not rewrites, "new AI output mixed with an undrained previous reply"
        duplex.drained.set()
        assert (await task)["ok"]
        await agent.leave()
    asyncio.run(run())


@pytest.mark.parametrize("repeat_caller_cancel", [False, True])
def test_caller_cancel_during_operation_cleanup_waits_then_resets_all_flags(tmp_path, repeat_caller_cancel):
    async def run():
        agent = make_agent(tmp_path)
        await agent.join("a", "c", source="auto", visit_id="v")
        rewriting = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        cleaned = []

        async def rewrite(kind, template):
            rewriting.set()
            await asyncio.Event().wait()

        async def stop_tts():
            cleaning.set()
            await release.wait()
            cleaned.append(True)
            return {"ok": True}

        agent.backend.rewrite_announcement = rewrite
        agent.duplex.stop_tts = stop_tts
        caller = asyncio.create_task(agent.speak_announcement("enter", "hi", expected_visit_id="v"))
        await rewriting.wait()
        operation = asyncio.create_task(agent._cancel_announcement())
        await cleaning.wait()
        caller.cancel()
        await asyncio.sleep(0)
        if repeat_caller_cancel:
            caller.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await operation
        assert cleaned, "second cancellation aborted the audio cleanup"
        assert not agent._announcement_active
        assert not agent._announcement_collecting
        assert not agent._reply_generating
        assert not agent._speaking
        assert agent._announcement_task is None
        assert not agent._announcement_cancel_requests
        await agent.join("manual", "room")
        input_received = []

        from channels.models import UtteranceResult
        def cut(audio):
            input_received.append(audio.pcm16)
            return UtteranceResult("accumulating")
        agent.ingress.cut = cut
        await agent._on_remote_pcm("human", b"\0\1", 16000)
        assert input_received == [b"\0\1"]
        await agent.leave()
    asyncio.run(run())
