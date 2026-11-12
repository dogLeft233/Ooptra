"""级联自动接话预算在排队、失败和重新进房时保持有界。"""
import asyncio

import pytest
from test_voice_operations import make_agent

from dialogue.models import VoiceReply


def test_budget_blocks_concurrent_and_failed_rounds_until_rejoin(tmp_path, monkeypatch):
    async def run():
        agent = make_agent(tmp_path)
        agent.settings.cascade_turn_limit = 3
        calls = []

        async def fail(*args, **kwargs):
            calls.append(1)
            await asyncio.sleep(0)
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(agent.backend, "handle_utterance", fail)
        try:
            await agent.join("a", "c")
            await asyncio.gather(*(
                agent._handle_utterance_reply("peer", b"\x01\x00", 16000)
                for _ in range(8)
            ))
            assert len(calls) == 3
            assert agent._cascade_limit_reached()
            # No audio/VAD/network work after exhaustion.
            monkeypatch.setattr(agent, "_vad_for", lambda uid: (_ for _ in ()).throw(
                AssertionError("exhausted budget reached VAD")))
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
        agent._bot.voice.voice_states = lambda: {"peer-oopz": {"cid": 12345}}
        received = []

        class Vad:
            def feed(self, pcm):
                received.append(pcm)
                return None

        monkeypatch.setattr(agent, "_vad_for", lambda uid: (
            Vad() if uid == "peer-oopz" else (_ for _ in ()).throw(AssertionError(uid))))
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

        async def reply(*args, **kwargs):
            calls.append(True)
            return VoiceReply(user_text="input", text="reply", pcm16=pcm if playback == "automatic" else b"")

        async def synthesize(text):
            return pcm, 24000

        async def push(*args, **kwargs):
            started.set()
            await release.wait()
            return {"ok": True}

        async def stop():
            stopped.append(True)
            return {"ok": True}

        monkeypatch.setattr(agent.backend, "handle_utterance", reply)
        monkeypatch.setattr(agent.backend, "tts", synthesize)
        agent.duplex.push_tts_pcm = push
        agent.duplex.stop_tts = stop
        worker = asyncio.create_task(agent._handle_utterance_reply("peer", b"\x01\x00", 16000))
        if playback == "manual":
            await worker
            worker = asyncio.create_task(agent.speak_text("manual"))
        await asyncio.wait_for(started.wait(), 1)
        assert agent.status()["cascade_attempts"] == 1
        monkeypatch.setattr(agent, "_vad_for", lambda uid: (_ for _ in ()).throw(
            AssertionError("exhausted budget reached recognition")))
        await agent._on_remote_pcm("peer", b"\x00\x40" * 320, 16000)
        release.set()
        await asyncio.wait_for(worker, 1)
        assert stopped == [True]
        assert not agent._speaking
        await agent._handle_utterance_reply("peer", b"\x01\x00", 16000)
        assert len(calls) == 1
        assert agent.status()["cascade_attempts"] == 1
        await agent.stop()

    asyncio.run(run())
