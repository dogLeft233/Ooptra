"""级联自动接话预算在排队、失败和重新进房时保持有界。"""
import asyncio

import pytest
from test_voice_operations import make_agent

from dialogue import create_expression_job
from dialogue.models import Recognition


def test_budget_blocks_concurrent_and_failed_rounds_until_rejoin(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.cascade_turn_limit = 3
        calls = []

        async def fail(*args, **kwargs):
            calls.append(1)
            await asyncio.sleep(0)
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(agent.backend, "recognize", fail)
        try:
            await agent.join("a", "c")
            await asyncio.gather(*(
                agent._handle_utterance_reply("peer", b"\x01\x00", 16000)
                for _ in range(8)
            ))
            assert len(calls) == 3
            assert agent._cascade_limit_reached()
            # No audio/VAD/network work after exhaustion.
            monkeypatch.setattr(agent.ingress, "accept", lambda *args: (_ for _ in ()).throw(
                AssertionError("exhausted budget reached capture")))
            await agent._on_remote_pcm("peer", b"\x01\x00", 16000)
            await agent.leave()
            await agent.join("a", "c")
            assert agent.status()["cascade_attempts"] == 0
            await agent._handle_utterance_reply("peer", b"\x01\x00", 16000)
            assert len(calls) == 4
        finally:
            await agent.stop()

    asyncio.run(run())


def test_peer_filter_resolves_agora_id_using_received_identity(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.listen_only_uids = ["peer-oopz"]
        from types import SimpleNamespace
        agent.bind_bot(SimpleNamespace(voice=SimpleNamespace(backend=None,
            voice_states=lambda: {"peer-oopz": {"cid":12345}})))
        received = []

        from channels.models import UtteranceResult
        def cut(audio):
            assert audio.user_key == "peer-oopz"
            received.append(audio.pcm16)
            return UtteranceResult("accumulating")
        monkeypatch.setattr(agent.ingress, "cut", cut)
        try:
            await agent._on_remote_pcm("12345", b"\x01\x00", 16000)
            assert received == [b"\x01\x00"]
            await agent._on_remote_pcm("99999", b"\x01\x00", 16000)
            assert len(received) == 1
        finally:
            await agent.stop()

    asyncio.run(run())


@pytest.mark.parametrize("playback", ["automatic", "manual"])
def test_exhausted_budget_still_allows_barge_in_without_new_recognition(tmp_path, monkeypatch, playback):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.cascade_turn_limit = 1
        agent.settings.barge_in = True
        await agent.join("a", "c")
        started, release = asyncio.Event(), asyncio.Event()
        calls, stopped = [], []
        pcm = b"\x01\x00" * 4000

        async def reply(audio, permit):
            calls.append(True)
            return Recognition(audio.input_ref, audio.session, permit.attempt_id, audio.user_key, "recognized", "input")
        def generate(intent, context, **kwargs):
            async def produce(emit):
                await emit("text_final", text="reply")
                if playback == "automatic" or intent.kind == "fixed":
                    await emit("audio", pcm16=pcm)
            return create_expression_job(intent.action_id, intent.session, produce, deadline=intent.deadline)

        async def synthesize(text):
            return pcm, 24000

        async def push(*args, **kwargs):
            started.set()
            await release.wait()
            return {"ok": True}

        async def stop():
            stopped.append(True)
            return {"ok": True}

        monkeypatch.setattr(agent.backend, "recognize", reply)
        monkeypatch.setattr(agent.backend, "generate", generate)
        monkeypatch.setattr(agent.backend, "tts", synthesize)
        agent.duplex.push_tts_pcm = push
        agent.duplex.stop_tts = stop
        worker = asyncio.create_task(agent._handle_utterance_reply("peer", b"\x01\x00" * 16000, 16000))
        if playback == "manual":
            await worker
            worker = asyncio.create_task(agent.speak_text("manual"))
        await asyncio.wait_for(started.wait(), 1)
        assert agent.status()["cascade_attempts"] == 1
        initial_stops = len(stopped)
        monkeypatch.setattr(agent.ingress, "cut", lambda uid: (_ for _ in ()).throw(
            AssertionError("exhausted budget reached recognition")))
        await agent._on_remote_pcm("peer", b"\x00\x40" * 320, 16000)
        release.set()
        await asyncio.wait_for(worker, 1)
        assert len(stopped) == initial_stops + 1
        assert not agent._speaking
        await agent._handle_utterance_reply("peer", b"\x01\x00" * 16000, 16000)
        assert len(calls) == 1
        assert agent.status()["cascade_attempts"] == 1
        await agent.stop()

    asyncio.run(run())
